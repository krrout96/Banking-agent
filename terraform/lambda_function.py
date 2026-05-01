import json
import boto3
import ipaddress
import math
import os
import re
from urllib.parse import quote_plus, unquote
from urllib.request import Request, urlopen

# --- Lambda Handler ---

def _is_bedrock_action_group_event(event):
    return isinstance(event, dict) and event.get("messageVersion") == "1.0" and "actionGroup" in event

def _extract_field(event, field_name):
    # 1) Direct payload style: { "payload": "...", "cidr": "..." }
    if isinstance(event, dict) and field_name in event:
        return event.get(field_name)

    # 2) Function details style: parameters array
    for param in event.get("parameters", []) if isinstance(event, dict) else []:
        if param.get("name") == field_name:
            return param.get("value")

    # 3) API schema style: requestBody.content.application/json.properties
    request_body = event.get("requestBody", {}) if isinstance(event, dict) else {}
    content = request_body.get("content", {}) if isinstance(request_body, dict) else {}
    for _, content_obj in content.items():
        for prop in content_obj.get("properties", []) if isinstance(content_obj, dict) else []:
            if prop.get("name") == field_name:
                return prop.get("value")

    return None

def _resolve_tool_name(event, context):
    tool_name = ""

    # 1) Try context (Bedrock Agent)
    try:
        if context and context.client_context and context.client_context.custom:
            tool_name = context.client_context.custom.get('bedrockAgentCoreToolName', '')
    except Exception:
        pass

    # 2) Bedrock action group API schema/function details
    if not tool_name and isinstance(event, dict):
        if event.get("function"):
            tool_name = event.get("function")
        elif event.get("apiPath"):
            path_to_tool = {
                "/cost-assistant": "cost_assistant",
                "/aws-docs-assistant": "aws_docs_assistant",
                "/vpc-subnet-calculator": "vpc_subnet_calculator",
            }
            tool_name = path_to_tool.get(event.get("apiPath"), "")

    # 3) Direct invoke style
    if not tool_name and isinstance(event, dict) and 'tool_name' in event:
        tool_name = event['tool_name']
    elif not tool_name and isinstance(event, dict) and 'body' in event:
        try:
            body_json = json.loads(event['body'])
            tool_name = body_json.get('tool_name', '')
        except Exception:
            pass

    if "___" in tool_name:
        tool_name = tool_name.split("___")[1]

    return tool_name

def _bedrock_response(event, status_code, result_text):
    api_path = event.get("apiPath") or "/unknown"
    http_method = event.get("httpMethod") or "POST"
    action_group = event.get("actionGroup") or "migration-tools"

    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": action_group,
            "apiPath": api_path,
            "httpMethod": http_method,
            "httpStatusCode": status_code,
            "responseBody": {
                "application/json": {
                    "body": json.dumps({"result": result_text})
                }
            }
        },
        "sessionAttributes": event.get("sessionAttributes", {}),
        "promptSessionAttributes": event.get("promptSessionAttributes", {})
    }

def lambda_handler(event, context):
    """
    Main entry point for the Lambda function.
    AgentCore Gateway routes the request here based on the tool name.
    """
    # Debug logging
    print("Received event:", json.dumps(event))
    
    tool_name = _resolve_tool_name(event, context)
    
    print(f"Routing to Tool: {tool_name}")
    
    try:
        if tool_name == 'cost_assistant':
            payload = _extract_field(event, "payload") or _extract_field(event, "service")
            result = cost_assistant(payload or event)
        elif tool_name == 'aws_docs_assistant':
            payload = _extract_field(event, "payload") or _extract_field(event, "query")
            result = aws_docs_assistant(payload or event)
        elif tool_name == 'vpc_subnet_calculator':
            cidr = _extract_field(event, "cidr")
            az_count = _extract_field(event, "az_count")
            tiers = _extract_field(event, "tiers")
            payload = {}
            if cidr:
                payload["cidr"] = cidr
            if az_count:
                payload["az_count"] = az_count
            if tiers:
                payload["tiers"] = tiers
            result = vpc_subnet_calculator(payload if payload else event)
        else:
            message = f"Unknown or missing tool name: '{tool_name}'."
            if _is_bedrock_action_group_event(event):
                return _bedrock_response(event, 400, message)
            return {'statusCode': 400, 'body': message}
            
        if _is_bedrock_action_group_event(event):
            return _bedrock_response(event, 200, result)
        return {'statusCode': 200, 'body': result}
    except Exception as e:
        print(f"Error executing {tool_name}: {e}")
        if _is_bedrock_action_group_event(event):
            return _bedrock_response(event, 500, f"Error executing tool: {str(e)}")
        return {'statusCode': 500, 'body': f"Error executing tool: {str(e)}"}

# --- Tool Implementations ---

REGION_TO_PRICING_LOCATION = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "ca-central-1": "Canada (Central)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-central-2": "Europe (Zurich)",
    "eu-north-1": "EU (Stockholm)",
    "eu-south-1": "EU (Milan)",
    "eu-south-2": "EU (Spain)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-south-2": "Asia Pacific (Hyderabad)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ap-southeast-4": "Asia Pacific (Melbourne)",
    "ap-east-1": "Asia Pacific (Hong Kong)",
    "sa-east-1": "South America (Sao Paulo)",
    "me-south-1": "Middle East (Bahrain)",
    "me-central-1": "Middle East (UAE)",
    "af-south-1": "Africa (Cape Town)"
}

SERVICE_CODE_ALIASES = {
    "ec2": "AmazonEC2",
    "amazon ec2": "AmazonEC2",
    "elastic compute cloud": "AmazonEC2",
    "instance": "AmazonEC2",
    "rds": "AmazonRDS",
    "amazon rds": "AmazonRDS",
    "aurora": "AmazonRDS",
    "lambda": "AWSLambda",
    "aws lambda": "AWSLambda",
    "s3": "AmazonS3",
    "amazon s3": "AmazonS3",
    "dynamodb": "AmazonDynamoDB",
    "amazon dynamodb": "AmazonDynamoDB",
    "ecs": "AmazonECS",
    "fargate": "AmazonECS",
    "elb": "AWSELB",
    "alb": "AWSELB",
    "nlb": "AWSELB",
    "load balancer": "AWSELB"
}

DIRECT_SERVICE_CODES = {
    "amazonec2": "AmazonEC2",
    "amazonrds": "AmazonRDS",
    "awslambda": "AWSLambda",
    "amazons3": "AmazonS3",
    "amazondynamodb": "AmazonDynamoDB",
    "amazonecs": "AmazonECS",
    "awselb": "AWSELB",
}

def _normalize_payload(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        return {"payload": text}
    return {"payload": str(payload)}

def _extract_region_from_text(text):
    if not text:
        return None
    match = re.search(r"\b[a-z]{2}-[a-z]+-\d\b", text.lower())
    return match.group(0) if match else None

def _extract_ec2_instance_type(text):
    if not text:
        return None
    match = re.search(r"\b([a-z][0-9][a-z0-9]*\.[a-z0-9]+)\b", text.lower())
    return match.group(1) if match else None

def _extract_rds_instance_type(text):
    if not text:
        return None
    match = re.search(r"\b(db\.[a-z0-9]+\.[a-z0-9]+)\b", text.lower())
    return match.group(1) if match else None

def _resolve_service_code(raw_service):
    if not raw_service:
        return None

    service_text = str(raw_service).strip().lower()
    if service_text in DIRECT_SERVICE_CODES:
        return DIRECT_SERVICE_CODES[service_text]

    if service_text in SERVICE_CODE_ALIASES:
        return SERVICE_CODE_ALIASES[service_text]

    for alias, service_code in SERVICE_CODE_ALIASES.items():
        if alias in service_text:
            return service_code

    return None

def _region_to_pricing_location(region_code):
    if not region_code:
        return None
    return REGION_TO_PRICING_LOCATION.get(region_code.lower())

def _extract_first_ondemand_price(price_item):
    terms = price_item.get("terms", {}).get("OnDemand", {})
    for term in terms.values():
        for dimension in term.get("priceDimensions", {}).values():
            usd = dimension.get("pricePerUnit", {}).get("USD")
            if usd:
                return {
                    "usd": usd,
                    "unit": dimension.get("unit", ""),
                    "description": dimension.get("description", "")
                }
    return None

def _maybe_monthly_cost(usd_price, unit):
    try:
        value = float(usd_price)
    except Exception:
        return None
    if unit.lower() in {"hrs", "hour", "hours", "hr"}:
        return round(value * 730, 4)
    return None

def _build_cost_query(payload):
    data = _normalize_payload(payload)
    raw_text = str(data.get("payload") or data.get("query") or data.get("service") or "").strip()

    service_raw = data.get("service") or data.get("service_name") or data.get("serviceCode") or raw_text
    service_code = _resolve_service_code(service_raw)

    region_code = (data.get("region") or data.get("aws_region") or _extract_region_from_text(raw_text) or "us-east-1").lower()
    location = data.get("location") or _region_to_pricing_location(region_code) or "US East (N. Virginia)"

    operating_system = str(data.get("operating_system") or data.get("os") or ("Windows" if "windows" in raw_text.lower() else "Linux"))
    instance_type = data.get("instance_type") or _extract_ec2_instance_type(raw_text)
    rds_instance_type = data.get("instance_type") or _extract_rds_instance_type(raw_text)
    database_engine = data.get("database_engine") or data.get("engine") or "MySQL"

    return {
        "service_code": service_code,
        "service_raw": str(service_raw or "").strip(),
        "region_code": region_code,
        "location": location,
        "operating_system": operating_system,
        "instance_type": instance_type,
        "rds_instance_type": rds_instance_type,
        "database_engine": database_engine
    }

def _build_pricing_filters(query):
    service_code = query["service_code"]
    filters = [{"Type": "TERM_MATCH", "Field": "location", "Value": query["location"]}]

    if service_code == "AmazonEC2":
        filters.extend([
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": query["instance_type"] or "m5.large"},
            {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": query["operating_system"]},
            {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
            {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
            {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
        ])
    elif service_code == "AmazonRDS":
        filters.extend([
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": query["rds_instance_type"] or "db.t3.medium"},
            {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": query["database_engine"]},
            {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": "Single-AZ"},
        ])
    elif service_code == "AWSLambda":
        filters.append({"Type": "TERM_MATCH", "Field": "group", "Value": "AWS-Lambda-Duration"})

    return filters

def _fetch_pricing_products(pricing_client, service_code, filters):
    attempts = [filters]
    # Relax optional filters if strict query returns no products.
    attempts.append([f for f in filters if f["Field"] in {"location", "instanceType", "databaseEngine", "group"}])
    attempts.append([f for f in filters if f["Field"] != "location"])

    seen = set()
    for active_filters in attempts:
        key = tuple((f["Field"], f["Value"]) for f in active_filters)
        if key in seen:
            continue
        seen.add(key)
        response = pricing_client.get_products(
            ServiceCode=service_code,
            Filters=active_filters,
            MaxResults=25
        )
        price_list = response.get("PriceList", [])
        if price_list:
            return price_list, active_filters
    return [], filters

def _format_cost_response(query, product, price, used_filters):
    attributes = product.get("product", {}).get("attributes", {})
    service_code = query["service_code"]
    monthly = _maybe_monthly_cost(price["usd"], price["unit"])

    lines = [
        f"AWS pricing estimate for service `{service_code}`",
        f"- Region: {query['region_code']} ({query['location']})",
        f"- Price: USD {price['usd']} per {price['unit'] or 'unit'}",
    ]

    if monthly is not None:
        lines.append(f"- Approx monthly (730 hours): USD {monthly}")

    if attributes.get("instanceType"):
        lines.append(f"- Instance type: {attributes.get('instanceType')}")
    if attributes.get("databaseEngine"):
        lines.append(f"- Database engine: {attributes.get('databaseEngine')}")
    if attributes.get("operatingSystem"):
        lines.append(f"- Operating system: {attributes.get('operatingSystem')}")
    if price.get("description"):
        lines.append(f"- Meter description: {price['description']}")

    lines.append("- Pricing source: AWS Pricing API (`pricing:GetProducts`)")
    lines.append(f"- Applied filters: {json.dumps(used_filters)}")

    return "\n".join(lines)

def cost_assistant(payload):
    """
    Cost assistant backed by AWS Pricing API.
    Supports payload as plain text or JSON string.

    Example payload JSON:
    {
      "service": "ec2",
      "region": "us-east-1",
      "instance_type": "m5.large",
      "operating_system": "Linux"
    }
    """
    pricing_client = boto3.client("pricing", region_name="us-east-1")
    query = _build_cost_query(payload)

    if not query["service_code"]:
        return (
            "Unable to determine AWS service for pricing request. "
            "Supported examples: EC2, RDS, Lambda, S3, DynamoDB, ECS/Fargate, ELB. "
            "You can also pass JSON payload like "
            "{\"service\":\"ec2\",\"region\":\"us-east-1\",\"instance_type\":\"m5.large\"}."
        )

    filters = _build_pricing_filters(query)

    try:
        price_list, used_filters = _fetch_pricing_products(pricing_client, query["service_code"], filters)
        if not price_list:
            return (
                f"No pricing products found for `{query['service_code']}` in `{query['location']}`. "
                "Try a more specific payload (for example, include `instance_type` for EC2 or RDS)."
            )

        parsed_products = []
        for item in price_list:
            try:
                parsed_products.append(json.loads(item) if isinstance(item, str) else item)
            except Exception:
                continue

        for product in parsed_products:
            price = _extract_first_ondemand_price(product)
            if price:
                return _format_cost_response(query, product, price, used_filters)

        return (
            "Pricing products were found, but no OnDemand USD price dimension was detected "
            "for the current filter set. Please try a more specific query."
        )
    except Exception as e:
        return (
            f"Error querying AWS Pricing API: {str(e)}. "
            "Ensure Lambda role has `pricing:GetProducts` permission."
        )

def _http_get(url, timeout_seconds=12):
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; MigrationAssistant/1.0)"
        }
    )
    with urlopen(req, timeout=timeout_seconds) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="ignore")

def _extract_docs_links(html_text, limit=5):
    if not html_text:
        return []

    raw_links = re.findall(r"https?://[^\s\"'<>]+", html_text)
    cleaned = []
    seen = set()
    for link in raw_links:
        candidate = unquote(link).rstrip(").,;\"'")
        if "docs.aws.amazon.com" not in candidate:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        cleaned.append(candidate)
        if len(cleaned) >= limit:
            break
    return cleaned

def _build_docs_query(payload):
    data = _normalize_payload(payload)
    query = str(data.get("query") or data.get("payload") or "").strip()
    return query

def aws_docs_assistant(payload):
    """
    Search AWS documentation pages in real time (without MCP runtime).
    """
    query = _build_docs_query(payload)
    if not query:
        return "Please provide a docs query, for example: `ECS blue/green deployment best practices`."

    encoded = quote_plus(query)
    search_urls = [
        f"https://docs.aws.amazon.com/search/doc-search.html?searchPath=documentation-guide&searchQuery={encoded}",
        f"https://aws.amazon.com/search/?searchQuery={encoded}&f-website-sections=docs",
    ]

    links = []
    errors = []
    for url in search_urls:
        try:
            html = _http_get(url)
            links.extend(_extract_docs_links(html, limit=8))
        except Exception as e:
            errors.append(f"{url}: {str(e)}")

    # Deduplicate and keep top N.
    deduped = []
    seen = set()
    for link in links:
        if link in seen:
            continue
        seen.add(link)
        deduped.append(link)
        if len(deduped) >= 5:
            break

    if deduped:
        result_lines = [f"AWS documentation results for: `{query}`"]
        for idx, link in enumerate(deduped, start=1):
            result_lines.append(f"{idx}. {link}")
        result_lines.append("Source: docs.aws.amazon.com search pages.")
        return "\n".join(result_lines)

    if errors:
        return (
            f"Unable to fetch AWS docs search results for `{query}` right now.\n"
            f"Network/query errors: {' | '.join(errors)}"
        )

    return (
        f"No docs results found for `{query}`. Try a more specific query, "
        "for example `Amazon RDS Multi-AZ failover`."
    )

def vpc_subnet_calculator(payload):
    """
    Calculates optimized VPC subnet ranges.
    Ported strictly from the original Migration Agent logic.
    """
    print(f"vpc_subnet_calculator called with payload: {payload}")
    
    try:
        # 1. Parse Input
        # Gateway might pass payload as a dict or a string depending on how it was invoked
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except:
                # If string input is just CIDR, use defaults
                if "/" in payload:
                    payload = {"cidr": payload}
                else:
                    return "Error: Please provide a valid JSON payload or CIDR string (e.g., '10.0.0.0/16')"
        
        # 2. Extract parameters
        vpc_cidr = payload.get("cidr")
        if not vpc_cidr:
            return "Error: strict 'cidr' parameter is required. Example: {'cidr': '10.0.0.0/16'}"

        az_count = int(payload.get("az_count", 2))
        tiers = payload.get("tiers", ["Public", "Private", "Database"])
        
        # 3. Calculation Logic
        
        # Calculate total subnets needed
        total_subnets_needed = len(tiers) * az_count
        
        # Calculate next power of 2 for splitting
        split_bits = math.ceil(math.log2(total_subnets_needed))
        
        # Create network object
        network = ipaddress.ip_network(vpc_cidr)
        new_prefix = network.prefixlen + split_bits
        
        if new_prefix > 30:
            return f"Error: CIDR {vpc_cidr} is too small to split into {total_subnets_needed} subnets."
            
        # Generate subnets
        subnets = list(network.subnets(new_prefix=new_prefix))
        
        # 4. Format Output
        output = [f"### 🌐 VPC Subnet Plan: {vpc_cidr}"]
        output.append(f"**Configuration**: {az_count} AZs, {len(tiers)} Tiers ({', '.join(tiers)})")
        output.append(f"**Subnet Mask**: /{new_prefix} ({subnets[0].num_addresses - 5} usable IPs per subnet)\n")
        
        output.append("| Tier | Availability Zone | CIDR Block | Usable IPs |")
        output.append("|---|---|---|---|")
        
        subnet_idx = 0
        az_names = ["a", "b", "c", "d", "e", "f"]
        
        for tier in tiers:
            for az_i in range(az_count):
                if subnet_idx < len(subnets):
                    sn = subnets[subnet_idx]
                    az_suffix = az_names[az_i % len(az_names)]
                    output.append(f"| {tier} | AZ-{az_suffix} | `{sn}` | {sn.num_addresses - 5} |")
                    subnet_idx += 1
        
        unused = len(subnets) - subnet_idx
        if unused > 0:
            output.append(f"\n*Remaining spare capacity: {unused} x /{new_prefix} subnets available for future expansion.*")
            
        result = "\n".join(output)
        return result

    except Exception as e:
        return f"Error executing vpc_subnet_calculator: {str(e)}"
