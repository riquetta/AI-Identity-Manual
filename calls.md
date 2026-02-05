
$headers = @{ "x-admin-key" = "dev-admin-key" }
$body = @{ 

agent_id="AGENT-cosmos1"; 
name="AGENTcosmos1"; 
roles="agent.chat.invoke"; 
enabled=$true 

} | ConvertTo-Json


Invoke-RestMethod -Method Post `
  -Uri "$baseUrl2/api/registry/register" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body


#discovery
Invoke-RestMethod -Method Get -Uri "$baseUrl2/api/registry/discover" | ConvertTo-Json

# Get agent
Invoke-RestMethod -Method Get -Uri "$baseUrl2/api/registry/agents/AGENT-2" | ConvertTo-Json


#Patch
$headers = @{ "x-admin-key" = "dev-admin-key" }
$body = @{ enabled = $false } | ConvertTo-Json

Invoke-RestMethod -Method Patch `
  -Uri "$baseUrl2/api/registry/agents/AGENT-cosmos" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body

# Delete
$headers = @{ "x-admin-key" = "dev-admin-key" }
Invoke-RestMethod -Method Delete `
  -Uri "$baseUrl2/api/registry/agents/AGENT-cosmos1" `
  -Headers $headers
