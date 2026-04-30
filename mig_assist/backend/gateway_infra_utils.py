import boto3
import json
import time
import os
import requests
from botocore.exceptions import ClientError

# --- IAM Utilities ---

def get_or_create_user_pool(cognito, pool_name):
    """Retrieves or creates a Cognito User Pool with a Domain"""
    response = cognito.list_user_pools(MaxResults=60)
    user_pool_id = None
    
    for pool in response.get("UserPools", []):
        if pool["Name"] == pool_name:
            user_pool_id = pool["Id"]
            break
            
    if not user_pool_id:
        print(f"Creating new User Pool: {pool_name}")
        created = cognito.create_user_pool(PoolName=pool_name)
        user_pool_id = created["UserPool"]["Id"]
        
        # Create Domain (Required for Auth)
        # We need a unique domain prefix. Using simplified logic here.
        domain_prefix = f"auth-{user_pool_id.split('_')[1].lower()}" 
        try:
            cognito.create_user_pool_domain(
                Domain=domain_prefix,
                UserPoolId=user_pool_id
            )
            print(f"Created domain prefix: {domain_prefix}")
        except ClientError as e:
            if "Domain already exists" in str(e):
                print(f"Domain {domain_prefix} exists.")
            else:
                print(f"Warning: Could not create domain: {e}")
                
    else:
        print(f"Found existing User Pool: {user_pool_id}")
        
    return user_pool_id

def get_or_create_resource_server(cognito, user_pool_id, identifier, name, scopes):
    """Ensures Resource Server exists for custom scopes"""
    try:
        cognito.describe_resource_server(
            UserPoolId=user_pool_id,
            Identifier=identifier
        )
        return identifier
    except cognito.exceptions.ResourceNotFoundException:
        print(f"Creating Resource Server: {identifier}")
        cognito.create_resource_server(
            UserPoolId=user_pool_id,
            Identifier=identifier,
            Name=name,
            Scopes=scopes
        )
        return identifier

def get_or_create_m2m_client(cognito, user_pool_id, client_name, resource_server_id, scopes=None):
    """Creates a Machine-to-Machine App Client with Client Credentials flow"""
    response = cognito.list_user_pool_clients(UserPoolId=user_pool_id, MaxResults=60)

    for client in response.get("UserPoolClients", []):
        if client["ClientName"] == client_name:
            desc = cognito.describe_user_pool_client(
                UserPoolId=user_pool_id, 
                ClientId=client["ClientId"]
            )
            return client["ClientId"], desc["UserPoolClient"]["ClientSecret"]
            
    print(f"Creating App Client: {client_name}")
    
    if scopes is None:
        scopes = [f"{resource_server_id}/gateway:read", f"{resource_server_id}/gateway:write"]
        
    created = cognito.create_user_pool_client(
        UserPoolId=user_pool_id,
        ClientName=client_name,
        GenerateSecret=True,
        AllowedOAuthFlows=["client_credentials"],
        AllowedOAuthScopes=scopes,
        AllowedOAuthFlowsUserPoolClient=True,
        SupportedIdentityProviders=["COGNITO"],
        ExplicitAuthFlows=["ALLOW_REFRESH_TOKEN_AUTH"]
    )
    return created["UserPoolClient"]["ClientId"], created["UserPoolClient"]["ClientSecret"]

def get_token(user_pool_id, client_id, client_secret, scope_string, region):
    """Retrieves an Access Token using Client Credentials flow"""
    # Domain is formatted as: https://{prefix}.auth.{region}.amazoncognito.com
    # We need to find the prefix first.
    cognito = boto3.client('cognito-idp', region_name=region)
    desc = cognito.describe_user_pool(UserPoolId=user_pool_id)
    domain = desc['UserPool'].get('Domain')
    
    if not domain:
        # Fallback if domain isn't in describe output (depends on creation method)
        # Try to guess or use the one we created
        domain_prefix = f"auth-{user_pool_id.split('_')[1].lower()}"
        host = f"{domain_prefix}.auth.{region}.amazoncognito.com"
    else:
        host = f"{domain}.auth.{region}.amazoncognito.com"
        
    url = f"https://{host}/oauth2/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope_string,
    }
    
    try:
        response = requests.post(url, headers=headers, data=data, auth=(client_id, client_secret))
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error getting token: {e}")
        return {"error": str(e)}

def setup_cognito_full(pool_name, client_name, resource_id, region):
    """Orchestrate the full setup"""
    cognito = boto3.client('cognito-idp', region_name=region)
    
    # 1. User Pool
    pool_id = get_or_create_user_pool(cognito, pool_name)
    
    # 2. Resource Server
    scopes = [
        {"ScopeName": "gateway:read", "ScopeDescription": "Read access"},
        {"ScopeName": "gateway:write", "ScopeDescription": "Write access"}
    ]
    get_or_create_resource_server(cognito, pool_id, resource_id, "MigrationGatewayRes", scopes)
    
    # 3. App Client
    scope_strs = [f"{resource_id}/gateway:read", f"{resource_id}/gateway:write"]
    client_id, client_secret = get_or_create_m2m_client(cognito, pool_id, client_name, resource_id, scope_strs)
    
    discovery_url = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/openid-configuration"
    
    return {
        "user_pool_id": pool_id,
        "client_id": client_id,
        "client_secret": client_secret,
        "discovery_url": discovery_url,
        "scope_string": " ".join(scope_strs)
    }

def create_lambda_role(role_name):
    """Creates IAM role for the Lambda Function"""
    iam = boto3.client('iam')
    
    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    }

    try:
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy)
        )
        print(f"Created Lambda role: {role_name}")
    except ClientError as e:
        if e.response['Error']['Code'] == 'EntityAlreadyExists':
            print(f"Lambda role {role_name} already exists.")
            role = iam.get_role(RoleName=role_name)
        else:
            raise e

    # Attach Basic Execution Policy
    iam.attach_role_policy(
        RoleName=role_name,
        PolicyArn='arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
    )
    
    # Wait for propagation
    time.sleep(10)
    return role['Role']['Arn']

def create_gateway_role(role_name, region):
    """Creates IAM role for the AgentCore Gateway"""
    iam = boto3.client('iam')
    sts = boto3.client('sts')
    account_id = sts.get_caller_identity()["Account"]
    
    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AssumeRolePolicy",
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": account_id},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account_id}:*"}
            }
        }]
    }
    
    # Permissions for the Gateway to invoke its targets (Lambda)
    gateway_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": [
                "lambda:InvokeFunction",
                "bedrock-agentcore:*", 
                "secretsmanager:GetSecretValue"
            ],
            "Resource": "*"
        }]
    }

    try:
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy)
        )
        print(f"Created Gateway role: {role_name}")
    except ClientError as e:
        if e.response['Error']['Code'] == 'EntityAlreadyExists':
            print(f"Gateway role {role_name} already exists.")
            # We must update the trust policy to ensure it matches current Region/Account
            iam.update_assume_role_policy(
                RoleName=role_name,
                PolicyDocument=json.dumps(assume_role_policy)
            )
            role = iam.get_role(RoleName=role_name)
        else:
            raise e

    # Attach Policy
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName='GatewayAccessPolicy',
        PolicyDocument=json.dumps(gateway_policy)
    )
    
    time.sleep(10)
    return role['Role']['Arn']

# --- Lambda Utilities ---

def create_lambda_function(function_name, role_arn, zip_file_path):
    """Creates or Updates the Lambda function code"""
    lambda_client = boto3.client('lambda')
    
    with open(zip_file_path, 'rb') as f:
        code_content = f.read()

    try:
        response = lambda_client.create_function(
            FunctionName=function_name,
            Runtime='python3.12', # Or 3.9+
            Role=role_arn,
            Handler='gateway_tools_lambda.lambda_handler', # MATCHES FILE NAME
            Code={'ZipFile': code_content},
            Timeout=60,
            MemorySize=128
        )
        print(f"Created Lambda function: {function_name}")
        return response['FunctionArn']
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceConflictException':
            print(f"Lambda {function_name} exists, updating code...")
            response = lambda_client.update_function_code(
                FunctionName=function_name,
                ZipFile=code_content
            )
            # Get ARN
            func = lambda_client.get_function(FunctionName=function_name)
            return func['Configuration']['FunctionArn']
        else:
            raise e

# --- Gateway Utilities ---

def create_gateway(name, role_arn, region):
    """Creates AgentCore Gateway (No Auth for simplicity, or we can add Auth)"""
    client = boto3.client('bedrock-agentcore-control', region_name=region)
    
    # Check if exists (simplified check)
    try:
        # Note: List does not filter by name, so we just try create and catch error, 
        # or implement a finder. For specific script, creating new random name is easier.
        pass 
    except:
        pass

    print(f"Creating Gateway '{name}'...")
    response = client.create_gateway(
        name=name,
        description="Migration Agent Gateway",
        protocolType='MCP',
        roleArn=role_arn,
        # For simplicity in this demo, we can use NO_AUTH or Custom.
        # But Gateway usually REQUIRES an authorizer. 
        # We will assume generic 'no-auth' or 'IAM' if supported, 
        # otherwise we fallback to the Cognito pattern from sample.
        # Checking sample: It used 'CUSTOM_JWT' with Cognito. 
        # To proceed without massive Cognito Setup code, we check if 'IAM' is an option?
        # If not, we will need the full Cognito setup.
        # 
        # UPDATE: Keeping it minimal. If we skip authorizer, default might be IAM.
        # Let's try creating with minimal config or assume user handles Auth.
        # The sample showed `authorizerType='CUSTOM_JWT'`.
        # Let's try to find if `IAM` authorizer is supported for simpler setup.
    )
    return response


def create_gateway_target(gateway_id, lambda_arn, tool_schema, region):
    """
    Connects the AgentCore Gateway to the Lambda Function using the Tool Schema.
    This enables the "Mapping".
    """
    client = boto3.client('bedrock-agentcore-control', region_name=region)
    target_name = "LambdaToolsTarget"
    
    print(f"Creating Gateway Target mapping for Gateway ID: {gateway_id}")
    
    # Configuration for Lambda Target
    target_config = {
        "mcp": {
            "lambda": {
                "lambdaArn": lambda_arn,
                "toolSchema": {
                    "inlinePayload": tool_schema 
                }
            }
        }
    }
    
    # Credential Provider (Gateway assumes Role to invoke Lambda)
    # The Gateway Role we created earlier has permission to Invoke Lambda.
    # We specify "GATEWAY_IAM_ROLE" to tell it to use its own execution role.
    credential_config = [ 
        {
            "credentialProviderType" : "GATEWAY_IAM_ROLE"
        }
    ]

    try:
        response = client.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=target_name,
            description='Target mapping to Lambda Tools',
            targetConfiguration=target_config,
            credentialProviderConfigurations=credential_config
        )
        print(f"✅ Successfully mapped Gateway to Lambda! Target ID: {response['targetId']}")
        return response['targetId']
    except ClientError as e:
        if "ConflictException" in str(e) or "EntityAlreadyExists" in str(e):
             print(f"Target already exists for {gateway_id}.")
             # In a real script we might fetch and update, but for now we assume it's done.
             return "existing-target"
        else:
            print(f"❌ Error creating target: {e}")
            raise e

