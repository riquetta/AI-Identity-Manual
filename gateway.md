``` mermaid

flowchart LR
  Caller["Caller (User, Service, Agent)"] -->|"Bearer JWT"| APIM["APIM Gateway (validate JWT and roles)"]
  APIM -->|"Inject headers: x-agent-appid, x-agent-roles"| Func["Azure Functions (/api/chat)"]
  Func -->|"Read registry"| File["agent_registry.json"]
  Func -->|"Call model"| AOAI["Azure OpenAI (Chat Completions)"]
  AOAI --> Func --> Caller
