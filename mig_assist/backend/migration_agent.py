import os
import io
import asyncio
import base64
import json
import logging
import re
import sys
import time
import traceback
from pathlib import Path
from uuid import uuid4

import boto3
import uvicorn
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from strands import Agent, tool
from strands.models import BedrockModel
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.types.exceptions import MaxTokensReachedException, ContextWindowOverflowException
from bedrock_agentcore.runtime import BedrockAgentCoreApp

import gateway_infra_utils as utils

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App & memory
# ---------------------------------------------------------------------------
app = BedrockAgentCoreApp()
GLOBAL_MEMORY_STORE: dict = {}


def add_to_memory(session_id: str, role: str, content: str) -> None:
    GLOBAL_MEMORY_STORE.setdefault(session_id, []).append(
        {"role": role, "content": content, "timestamp": time.time()}
    )


def get_memory(session_id: str, limit: int = 10) -> list:
    return GLOBAL_MEMORY_STORE.get(session_id, [])[-limit:]


# ---------------------------------------------------------------------------
# Gateway / Lambda tool helper
# ---------------------------------------------------------------------------
GATEWAY_URL = os.getenv("GATEWAY_URL")
CURRENT_IMAGE_CONTEXT: dict = {}


def get_dynamic_token() -> str | None:
    try:
        user_pool_id = os.getenv("GATEWAY_USER_POOL_ID")
        client_id = os.getenv("GATEWAY_CLIENT_ID")
        client_secret = os.getenv("GATEWAY_CLIENT_SECRET")
        scope_string = os.getenv("GATEWAY_SCOPE_STRING", "")

        if not (user_pool_id and client_id and client_secret):
            if os.path.exists("gateway_auth.json"):
                with open("gateway_auth.json") as f:
                    cfg = json.load(f)
                user_pool_id = user_pool_id or cfg.get("user_pool_id")
                client_id = client_id or cfg.get("client_id")
                client_secret = client_secret or cfg.get("client_secret")
                scope_string = scope_string or cfg.get("scope_string", "")

        if not (user_pool_id and client_id and client_secret):
            logger.warning("Gateway auth credentials not found.")
            return None

        resp = utils.get_token(
            user_pool_id=user_pool_id,
            client_id=client_id,
            client_secret=client_secret,
            scope_string=scope_string,
            region=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        )
        return resp.get("access_token")
    except Exception as e:
        logger.error(f"Failed to fetch gateway token: {e}")
        return None


def invoke_gateway_tool(tool_name: str, payload) -> str:
    function_name = os.getenv("TOOLS_LAMBDA_NAME", "migration-agent-cloud-tools")
    lambda_payload = {"tool_name": tool_name}
    if isinstance(payload, dict):
        lambda_payload.update(payload)
    else:
        lambda_payload["payload"] = payload

    try:
        client = boto3.client("lambda", region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
        response = client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(lambda_payload),
        )
        data = json.loads(response["Payload"].read())
        if "FunctionError" in response:
            logger.error(f"Lambda error: {data}")
            return f"Tool execution failed: {data}"
        return data.get("body", str(data))
    except Exception as e:
        logger.error(f"Lambda invoke failed [{tool_name}]: {e}")
        return f"Error invoking tool: {e}"


# ---------------------------------------------------------------------------
# Diagram generation
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DIAGRAM_OUTPUT_DIR = Path(SCRIPT_DIR) / "generated-diagrams"
DIAGRAM_OUTPUT_DIR.mkdir(exist_ok=True)
# Ensure the nginx-served diagrams dir exists in the container
Path(SCRIPT_DIR) / "static" / "diagrams"
(Path(SCRIPT_DIR) / "static" / "diagrams").mkdir(parents=True, exist_ok=True)

# Ensure Graphviz binary is on PATH (Windows local dev)
for _gv_path in [r"C:\Program Files\Graphviz\bin", r"C:\Program Files (x86)\Graphviz\bin"]:
    if os.path.isdir(_gv_path) and _gv_path not in os.environ.get("PATH", ""):
        os.environ["PATH"] = os.environ["PATH"] + os.pathsep + _gv_path

logger.info(f"[Config] DIAGRAM_BUCKET_NAME = {os.getenv('DIAGRAM_BUCKET_NAME', '<not set>')}")

_ARCH_JSON_PROMPT = """\
Extract the AWS architecture from the user request and return ONLY valid JSON:
{{
  "title": "Architecture title",
  "clusters": [{{"name": "Cluster label", "services": ["Service1", "Service2"]}}],
  "connections": [["SourceService", "TargetService"]]
}}
Use real AWS service names. Include ALL services mentioned. No explanation.

User request:
{payload}"""

_AWS_COLORS = {
    "default":    {"bg": "#E8F4FD", "border": "#1A73E8", "text": "#0D47A1"},
    "compute":    {"bg": "#FFF3E0", "border": "#FF6D00", "text": "#E65100"},
    "network":    {"bg": "#E8F5E9", "border": "#2E7D32", "text": "#1B5E20"},
    "database":   {"bg": "#F3E5F5", "border": "#6A1B9A", "text": "#4A148C"},
    "storage":    {"bg": "#FFF8E1", "border": "#F57F17", "text": "#E65100"},
    "security":   {"bg": "#FCE4EC", "border": "#C62828", "text": "#B71C1C"},
    "management": {"bg": "#E0F2F1", "border": "#00695C", "text": "#004D40"},
}
_SERVICE_CATEGORY = {
    "EC2": "compute", "ECS": "compute", "ECS Fargate": "compute",
    "Lambda": "compute", "Fargate": "compute",
    "ALB": "network", "NLB": "network", "Route 53": "network",
    "CloudFront": "network", "API Gateway": "network", "NAT Gateway": "network",
    "Transit Gateway": "network", "VPC Endpoint": "network",
    "Network Firewall": "network", "WAF": "network",
    "IGW": "network", "Internet Gateway": "network",
    "RDS": "database", "Aurora": "database", "DynamoDB": "database",
    "ElastiCache": "database", "Redshift": "database",
    "S3": "storage", "EFS": "storage", "EBS": "storage",
    "Cognito": "security", "IAM": "security", "KMS": "security",
    "Secrets Manager": "security", "Shield": "security",
    "CloudWatch": "management", "CloudTrail": "management",
    "SQS": "management", "SNS": "management",
}


def _render_architecture_png(title: str, clusters: list, connections: list) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    all_services: dict = {}
    cluster_colors = [
        "#E3F2FD", "#E8F5E9", "#FFF3E0", "#F3E5F5",
        "#E0F2F1", "#FFF8E1", "#FCE4EC", "#EDE7F6",
    ]
    x_cursor = 0.5
    cluster_boxes = []

    for ci, cluster in enumerate(clusters):
        services = cluster.get("services", [])
        y_start = len(services) * 1.4 + 0.8
        for si, svc in enumerate(services):
            all_services[svc] = (x_cursor, y_start - si * 1.4 - 0.7)
        cluster_boxes.append({
            "name": cluster["name"], "x": x_cursor,
            "y_top": y_start + 0.3,
            "y_bot": y_start - len(services) * 1.4 + 0.1,
            "color": cluster_colors[ci % len(cluster_colors)],
        })
        x_cursor += 2.2

    total_w = max(x_cursor + 0.5, 8)
    total_h = max(
        max((len(c.get("services", [])) for c in clusters), default=3) * 1.4 + 2.0, 6
    )

    fig, ax = plt.subplots(figsize=(max(total_w * 0.9, 10), max(total_h * 0.75, 6)))
    ax.set_xlim(-0.5, total_w)
    ax.set_ylim(-0.5, total_h + 0.5)
    ax.axis("off")
    fig.patch.set_facecolor("#F8FAFC")
    ax.set_facecolor("#F8FAFC")
    ax.text(total_w / 2, total_h + 0.1, title, ha="center", va="top",
            fontsize=14, fontweight="bold", color="#1A237E")

    for cb in cluster_boxes:
        ax.add_patch(FancyBboxPatch(
            (cb["x"] - 0.9, cb["y_bot"] - 0.2), 1.8, cb["y_top"] - cb["y_bot"] + 0.2,
            boxstyle="round,pad=0.1", linewidth=1.5,
            edgecolor="#90A4AE", facecolor=cb["color"], alpha=0.6, zorder=1,
        ))
        ax.text(cb["x"], cb["y_top"] + 0.05, cb["name"],
                ha="center", va="bottom", fontsize=7.5,
                color="#37474F", fontweight="bold", style="italic")

    for src, dst in connections:
        if src in all_services and dst in all_services:
            x1, y1 = all_services[src]
            x2, y2 = all_services[dst]
            ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                        arrowprops=dict(arrowstyle="-|>", color="#546E7A", lw=1.4,
                                        connectionstyle="arc3,rad=0.08"), zorder=2)

    for svc, (x, y) in all_services.items():
        colors = _AWS_COLORS.get(
            _SERVICE_CATEGORY.get(svc, "default"), _AWS_COLORS["default"]
        )
        ax.add_patch(FancyBboxPatch(
            (x - 0.75, y - 0.38), 1.5, 0.76,
            boxstyle="round,pad=0.08", linewidth=1.8,
            edgecolor=colors["border"], facecolor=colors["bg"], zorder=3,
        ))
        label = svc if len(svc) <= 16 else svc.replace(" ", "\n", 1)
        ax.text(x, y, label, ha="center", va="center", fontsize=7.5,
                fontweight="bold", color=colors["text"], zorder=4, multialignment="center")

    plt.tight_layout(pad=0.5)
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _save_diagram_image(image_bytes: bytes, ext: str = "png") -> str | None:
    fname = f"diagram_{uuid4().hex[:8]}_{int(time.time())}.{ext}"
    bucket_name = os.getenv("DIAGRAM_BUCKET_NAME")

    if bucket_name:
        try:
            s3 = boto3.client("s3")
            s3_key = f"diagrams/{fname}"
            s3.put_object(Bucket=bucket_name, Key=s3_key,
                          Body=image_bytes, ContentType=f"image/{ext}")
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket_name, "Key": s3_key},
                ExpiresIn=86400,  # 24 hours
            )
            logger.info(f"[S3] Uploaded: s3://{bucket_name}/{s3_key}")
            return url
        except Exception as e:
            logger.error(f"[S3] Upload failed: {e}")

    for candidate in [
        Path(SCRIPT_DIR) / "static" / "diagrams",
        Path(SCRIPT_DIR).parent / "frontend" / "public" / "diagrams",
    ]:
        if candidate.parent.exists():
            candidate.mkdir(parents=True, exist_ok=True)
            dest = candidate / fname
            dest.write_bytes(image_bytes)
            logger.info(f"[Local] Saved: {dest}")
            return f"/diagrams/{fname}"

    logger.error("[Save] No valid storage location found.")
    return None


def _generate_diagram(payload: str) -> str:
    """
    Ask Nova Pro to extract architecture as JSON, render with diagrams library (preferred)
    or matplotlib (fallback), save to S3. Returns markdown image link or error message.
    """
    bedrock = boto3.client("bedrock-runtime",
                           region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
    arch_json = None
    try:
        resp = bedrock.invoke_model(
            modelId="us.amazon.nova-pro-v1:0",
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "messages": [{"role": "user", "content": [
                    {"text": _ARCH_JSON_PROMPT.format(payload=payload)}
                ]}],
                "inferenceConfig": {"max_new_tokens": 800, "temperature": 0.1},
            }),
        )
        text = (json.loads(resp["body"].read())
                .get("output", {}).get("message", {})
                .get("content", [{}])[0].get("text", ""))
        logger.info(f"[diagram] Nova response ({len(text)} chars)")

        # Strategy 1: fenced ```json ... ``` block
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if m:
            arch_json = json.loads(m.group(1))
        else:
            # Strategy 2: find the outermost { ... } block
            for start_m in reversed(list(re.finditer(r"\{", text))):
                start = start_m.start()
                depth = 0
                for i, ch in enumerate(text[start:]):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            arch_json = json.loads(text[start:start + i + 1])
                            break
                if arch_json:
                    break

        logger.info(f"[diagram] {len(arch_json.get('clusters', []))} clusters, "
                    f"{len(arch_json.get('connections', []))} connections")
    except Exception as e:
        logger.warning(f"[diagram] JSON extraction failed: {e}")

    if arch_json:
        # Try diagrams library first (real AWS icons), fall back to matplotlib
        image_bytes = _render_with_diagrams_library(arch_json)
        if not image_bytes:
            logger.info("[diagram] diagrams library unavailable — using matplotlib fallback")
            try:
                image_bytes = _render_architecture_png(
                    title=arch_json.get("title", "AWS Architecture"),
                    clusters=arch_json.get("clusters", []),
                    connections=arch_json.get("connections", []),
                )
            except Exception as e:
                logger.warning(f"[diagram] matplotlib render failed: {e}")
                image_bytes = None

        if image_bytes:
            img_url = _save_diagram_image(image_bytes, "png")
            if img_url:
                return f"### Generated Architecture Diagram:\n\n![Architecture Diagram]({img_url})\n"

    logger.warning("[diagram] Diagram generation failed — no image produced.")
    return "I was unable to generate the diagram. Please try again with more details about the architecture."


def _render_with_diagrams_library(arch_json: dict) -> bytes | None:
    """
    Render using the `diagrams` library with real AWS service icons.
    Returns PNG bytes or None if library unavailable.
    """
    try:
        from diagrams import Diagram, Cluster
        from diagrams.aws.compute import EC2, ECS, Fargate, Lambda
        from diagrams.aws.network import (
            ALB, NLB, Route53, CloudFront, APIGateway,
            NATGateway, TransitGateway, Endpoint, NetworkFirewall,
            InternetGateway, IGW, TGW, VPC, PrivateSubnet, PublicSubnet,
        )
        from diagrams.aws.database import RDS, Aurora, Dynamodb, ElastiCache, Redshift
        from diagrams.aws.storage import S3, EFS, EBS
        from diagrams.aws.security import Cognito, IAM, KMS, SecretsManager, Shield, WAF
        from diagrams.aws.management import Cloudwatch, Cloudtrail, Config
        from diagrams.aws.integration import SQS, SNS
        import tempfile
    except ImportError:
        return None

    SERVICE_MAP = {
        # Compute
        "EC2": EC2, "ECS": ECS, "ECS Fargate": Fargate, "Fargate": Fargate,
        "Lambda": Lambda, "Amazon EC2": EC2,
        "Amazon EC2 (Public Subnet)": EC2, "Amazon EC2 (Private Subnet)": EC2,
        # Network
        "ALB": ALB, "NLB": NLB, "Route 53": Route53, "CloudFront": CloudFront,
        "API Gateway": APIGateway,
        "NAT Gateway": NATGateway, "Transit Gateway": TransitGateway, "TGW": TGW,
        "VPC Endpoint": Endpoint, "VPC Endpoint (S3)": Endpoint,
        "VPC Endpoint (DynamoDB)": Endpoint, "Endpoint": Endpoint,
        "Network Firewall": NetworkFirewall, "AWS Network Firewall": NetworkFirewall,
        "IGW": IGW, "Internet Gateway": InternetGateway,
        "VPC": VPC, "Amazon VPC": VPC,
        "Public Subnet": PublicSubnet, "Private Subnet": PrivateSubnet,
        "WAF": WAF,
        # Database
        "RDS": RDS, "Aurora": Aurora, "DynamoDB": Dynamodb, "Dynamodb": Dynamodb,
        "ElastiCache": ElastiCache, "Redshift": Redshift,
        # Storage
        "S3": S3, "EFS": EFS, "EBS": EBS,
        # Security
        "Cognito": Cognito, "IAM": IAM, "KMS": KMS,
        "Secrets Manager": SecretsManager, "Shield": Shield,
        # Management
        "CloudWatch": Cloudwatch, "Cloudwatch": Cloudwatch,
        "CloudTrail": Cloudtrail, "Config": Config,
        # Integration
        "SQS": SQS, "SNS": SNS,
    }

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "diagram"
            title = arch_json.get("title", "AWS Architecture")
            clusters = arch_json.get("clusters", [])
            connections = arch_json.get("connections", [])
            nodes = {}

            with Diagram(title, filename=str(output_path), show=False,
                         direction="LR", graph_attr={"dpi": "150"}):
                for cluster_def in clusters:
                    services = cluster_def.get("services", [])
                    if not services:
                        continue
                    with Cluster(cluster_def.get("name", "Services")):
                        for svc in services:
                            svc_class = SERVICE_MAP.get(svc, EC2)
                            nodes[svc] = svc_class(svc)

                for src, dst in connections:
                    if src in nodes and dst in nodes:
                        nodes[src] >> nodes[dst]

            png_path = output_path.with_suffix(".png")
            if png_path.exists():
                logger.info(f"[diagrams] AWS icons rendered ({png_path.stat().st_size} bytes)")
                return png_path.read_bytes()

    except Exception as e:
        logger.warning(f"[diagrams] Render failed: {e}")

    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@tool
def hld_lld_input_agent(payload: str) -> str:
    """Analyzes HLD/LLD architecture images. Pass 'IMAGE_PAYLOAD' to use the uploaded image."""
    if payload == "IMAGE_PAYLOAD":
        payload = CURRENT_IMAGE_CONTEXT.get("payload", "")
        if not payload:
            return "Error: No image found in current context."

    image_data = payload
    image_format = "png"
    if isinstance(image_data, str):
        if "image/jpeg" in image_data or image_data.strip().startswith("/9j/"):
            image_format = "jpeg"
        if "," in image_data:
            image_data = image_data.split(",")[1]
        try:
            image_bytes = base64.b64decode(image_data)
        except Exception as e:
            return f"Error decoding image: {e}"
    else:
        image_bytes = image_data

    try:
        bedrock = boto3.client("bedrock-runtime",
                               region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
        resp = bedrock.invoke_model(
            modelId="us.amazon.nova-pro-v1:0",
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "messages": [{"role": "user", "content": [
                    {"text": ("Analyze this HLD/LLD architecture diagram. Identify: "
                              "1) Components and relationships, 2) AWS equivalent services, "
                              "3) Security considerations, 4) Scalability aspects. "
                              "Provide a detailed analysis for cloud migration planning.")},
                    {"image": {"format": image_format,
                               "source": {"bytes": base64.b64encode(image_bytes).decode()}}}
                ]}],
                "inferenceConfig": {"max_new_tokens": 2000, "temperature": 0.1},
            }),
        )
        result = json.loads(resp["body"].read())
        return result["output"]["message"]["content"][0]["text"]
    except Exception as e:
        logger.error(f"HLD/LLD analysis error: {e}")
        return f"Error analyzing image: {e}"


@tool
def arch_diag_assistant(payload: str) -> str:
    """Creates a professional AWS architecture diagram as a PNG image."""
    logger.info(f"arch_diag_assistant called: {payload[:100]}...")
    return _generate_diagram(payload)


@tool
def cost_assistant(service_name: str) -> str:
    """Estimates AWS service costs (e.g. 'EC2', 'RDS', 'Lambda')."""
    return invoke_gateway_tool("cost_assistant", {"payload": service_name})


@tool
def aws_docs_assistant(query: str) -> str:
    """Searches AWS documentation for best practices and architectural patterns."""
    return invoke_gateway_tool("aws_docs_assistant", {"payload": query})


@tool
def vpc_subnet_calculator(cidr_block: str) -> str:
    """Calculates VPC subnet divisions for a given CIDR block (e.g. '10.0.0.0/16')."""
    return invoke_gateway_tool("vpc_subnet_calculator", {"cidr": cidr_block})


# ---------------------------------------------------------------------------
# Agent system prompt
# ---------------------------------------------------------------------------
MIGRATION_SYSTEM_PROMPT = """\
You are an expert AWS Migration Specialist and Cloud Architect.

### Responsibilities
1. Analyze the user's existing infrastructure. Use `hld_lld_input_agent` if an image is provided.
2. Ask clarifying questions about preferences (Serverless vs Containers, Managed vs Self-hosted).
3. Recommend migration strategies (Re-host, Re-platform, Re-factor) and AWS services.
4. Use `cost_assistant` for TCO estimates and `aws_docs_assistant` for documentation.
5. Always recommend minimal viable subnet sizes. Use `vpc_subnet_calculator` for IP planning.

### Rules
- Use `arch_diag_assistant` ONLY when the user explicitly asks for a diagram or visual.
- If `arch_diag_assistant` returns a Markdown image link, include it VERBATIM in your response.
- Break complex migrations into logical phases.
- Be professional, encouraging, and technically precise.
"""

ALL_TOOLS = [
    cost_assistant,
    aws_docs_assistant,
    vpc_subnet_calculator,
    hld_lld_input_agent,
    arch_diag_assistant,
]


# ---------------------------------------------------------------------------
# Agent invocation helpers
# ---------------------------------------------------------------------------
def invoke_local_agent(prompt_text: str) -> str:
    agent = Agent(
        model=BedrockModel(model_id="us.amazon.nova-pro-v1:0", max_tokens=4096),
        system_prompt=MIGRATION_SYSTEM_PROMPT,
        tools=ALL_TOOLS,
        conversation_manager=SlidingWindowConversationManager(window_size=10),
    )
    try:
        response = agent(prompt_text)
    except (MaxTokensReachedException, ContextWindowOverflowException):
        logger.warning("Context overflow — retrying with fresh agent.")
        response = Agent(
            model=BedrockModel(model_id="us.amazon.nova-pro-v1:0", max_tokens=4096),
            system_prompt=MIGRATION_SYSTEM_PROMPT,
            tools=ALL_TOOLS,
        )(prompt_text)

    parts = response.message.get("content", []) if getattr(response, "message", None) else []
    text = "\n".join(
        p["text"] for p in parts if isinstance(p, dict) and p.get("text")
    ).strip()
    return text or str(response)


def invoke_bedrock_agent(prompt_text: str, session_id: str) -> str | None:
    agent_id = os.getenv("BEDROCK_AGENT_ID", "").strip()
    alias_id = os.getenv("BEDROCK_AGENT_ALIAS_ID", "").strip()
    if not (agent_id and alias_id):
        return None

    try:
        runtime = boto3.client("bedrock-agent-runtime",
                               region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
        response = runtime.invoke_agent(
            agentId=agent_id, agentAliasId=alias_id,
            sessionId=session_id, inputText=prompt_text,
        )
    except ClientError as e:
        logger.error(f"Bedrock Agent invoke failed: {e}")
        return None

    chunks = []
    for event in response.get("completion", []):
        chunk = event.get("chunk")
        if chunk and "bytes" in chunk:
            raw = chunk["bytes"]
            chunks.append(raw.decode("utf-8", errors="ignore") if isinstance(raw, (bytes, bytearray)) else str(raw))
    return "".join(chunks).strip() or None


# ---------------------------------------------------------------------------
# Request classification helpers
# ---------------------------------------------------------------------------
# Any request containing these phrases goes directly to arch_diag_assistant
_DIAGRAM_TRIGGERS = {
    "diagram", "architecture diagram", "architecture design", "aws diagram",
    "draw", "redraw", "flowchart", "visual", "aws icon", "icons",
    "generate image", "architecture image", "hld", "lld", "png",
}
_GENERATION_VERBS = {
    "generate", "create", "draw", "build", "produce", "show", "design",
    "modify", "update", "redraw", "revise", "enhance", "add", "convert",
    "make", "give me", "need", "want",
}
_DIAGRAM_NOUNS = {
    "diagram", "architecture", "image", "visual", "flowchart",
    "png", "icon", "icons", "design",
}


def _is_diagram_request(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in _DIAGRAM_TRIGGERS)


def _is_diagram_generation_request(text: str) -> bool:
    t = text.lower()
    # Direct trigger match — no verb required
    if any(trigger in t for trigger in _DIAGRAM_TRIGGERS):
        return True
    # Verb + noun fallback
    return any(v in t for v in _GENERATION_VERBS) and any(n in t for n in _DIAGRAM_NOUNS)


def _has_image_link(text: str) -> bool:
    return bool(re.search(r"!\[[^\]]*\]\([^)]+\)", text or ""))


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------
@app.entrypoint
async def migration_assistant(payload):
    if isinstance(payload, str):
        user_input, user_id, context = payload, "unknown", {}
    else:
        user_input = payload.get("input") or payload.get("prompt") or ""
        user_id = payload.get("user_id", "unknown")
        context = payload.get("context", {})

    session_id = context.get("session_id") or f"session_{user_id}_{int(time.time())}"
    original_input = user_input
    logger.info(f"Session: {session_id} | Input: {user_input[:80]}...")

    # Inject condensed history to avoid token bloat
    past = get_memory(session_id)
    if past:
        recent = past[-6:]
        history = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Assistant'}: "
            f"{m['content'][:300]}{'...' if len(m['content']) > 300 else ''}"
            for m in recent
        )
        user_input = f"[Recent context]\n{history}\n\n[Current message]\n{user_input}"

    # Handle image upload
    image_data = payload.get("image_base64") if isinstance(payload, dict) else None
    if image_data:
        CURRENT_IMAGE_CONTEXT["payload"] = image_data
        user_input += "\n\n[System]: Image uploaded. Pass 'IMAGE_PAYLOAD' to hld_lld_input_agent."
    else:
        CURRENT_IMAGE_CONTEXT["payload"] = None

    wants_diagram = _is_diagram_generation_request(original_input)
    needs_local = bool(image_data) or _is_diagram_request(original_input)

    try:
        loop = asyncio.get_running_loop()

        if wants_diagram:
            # Bypass agent — call diagram tool directly
            response_text = await loop.run_in_executor(None, arch_diag_assistant, original_input)
        elif needs_local:
            response_text = await loop.run_in_executor(None, invoke_local_agent, user_input)
        else:
            response_text = await loop.run_in_executor(None, invoke_bedrock_agent, user_input, session_id)
            if not response_text:
                response_text = await loop.run_in_executor(None, invoke_local_agent, user_input)

        # Safety net: if diagram was requested but no image returned, force it
        if wants_diagram and not _has_image_link(response_text or ""):
            logger.warning("No image in diagram response — retrying directly.")
            response_text = await loop.run_in_executor(None, arch_diag_assistant, original_input)

        add_to_memory(session_id, "user", original_input)
        add_to_memory(session_id, "assistant", response_text or "")
        return response_text

    except (MaxTokensReachedException, ContextWindowOverflowException):
        return "Context limit reached. Please start a new session to continue."
    except Exception as e:
        logger.error(f"Agent error: {e}\n{traceback.format_exc()}")
        return f"Server Error: {e}"


if __name__ == "__main__":
    logger.info(f"DIAGRAM_BUCKET_NAME = {os.getenv('DIAGRAM_BUCKET_NAME', '<not set>')}")
    uvicorn.run(app, host="0.0.0.0", port=8081)
