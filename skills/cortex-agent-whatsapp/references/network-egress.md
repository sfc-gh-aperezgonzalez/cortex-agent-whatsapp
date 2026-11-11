# Fixed egress IP for the Lambda

The bot service user authenticates with a PAT, and Snowflake only lets a `TYPE=SERVICE` user create or use a
PAT while a network policy applies to it. The network rule needs the Lambda's public egress IP, so that IP
must be **fixed**.

| Lambda placement | Egress IP | Works with a network policy? |
|---|---|---|
| No VPC (default) | AWS-owned, changes on cold starts | No |
| Private subnets → NAT gateway with an Elastic IP | The Elastic IP, stable | Yes (recommended) |
| Private subnets → transit gateway / corporate egress | Your corporate NAT IP(s), stable | Yes |
| Public subnet | Lambda gets no public IP, so no internet access | No |

## Find an existing fixed-egress path

```bash
# Private subnets whose default route goes to a NAT gateway (or a transit gateway)
aws ec2 describe-route-tables --filters Name=vpc-id,Values=<vpc-id> \
  --query 'RouteTables[].{assoc:Associations[].SubnetId,nat:Routes[?DestinationCidrBlock==`0.0.0.0/0`].NatGatewayId|[0],tgw:Routes[?DestinationCidrBlock==`0.0.0.0/0`].TransitGatewayId|[0]}'
aws ec2 describe-nat-gateways --filter Name=vpc-id,Values=<vpc-id> \
  --query 'NatGateways[].{id:NatGatewayId,subnet:SubnetId,ip:NatGatewayAddresses[0].PublicIp,state:State}'
```

Pick two private subnets in different AZs that route `0.0.0.0/0` to the same NAT (or transit gateway), plus
a security group that allows outbound HTTPS. Then deploy and **measure**: `deploy_lambda.sh egress 3` must
print the same IP for three fresh cold starts. Only that measured IP goes into the network rule. With a
transit gateway the public IP is not visible in AWS at all, so measuring is the only way.

## Create one (costs money; ask first)

If there is no NAT, creating one costs roughly USD 35/month per NAT gateway plus data processing. Steps:
Elastic IP → NAT gateway in a **public** subnet → route table for two private subnets with `0.0.0.0/0 → nat`.
Confirm with the user before creating it and give them the teardown commands.

## Account-level network policy

```sql
SHOW PARAMETERS LIKE 'NETWORK_POLICY' IN ACCOUNT;
```

If the account already has a policy (common: VPN or office IPs only), the user-level policy from
`02_network_policy.sql` overrides it **for the bot user only**. Nothing changes for other users.

## Not recommended: relax the requirement

An authentication policy with `PAT_POLICY = (NETWORK_POLICY_EVALUATION = ENFORCED_NOT_REQUIRED)` lets a
service user use a PAT without a network policy. The PAT then works from any IP, so anyone who obtains it
can call Snowflake as the bot. Only consider it for a short-lived throwaway demo, and say so explicitly.
