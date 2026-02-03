``` mermaid
flowchart TD
  C1["Client (Browser, Postman, Service)"] -->|HTTPS| AZF["Azure Functions FunctionApp"]

  AZF --> R1["Route: /api/defaultroute (GET, POST)"]
  AZF --> R2["Route: /api/registry/discover (GET)"]
  AZF --> R3["Route: /api/registry/register (POST)"]
  AZF --> R4["Route: /api/chat (POST)"]

  %% defaultroute
  R1 --> DR["fastapientra(req)"]
  DR --> DR2{"Has name?"}
  DR2 -- Yes --> DR_OK["200 Hello {name}"]
  DR2 -- No --> DR_MSG["200 Generic message"]

  %% registry discover
  R2 --> DISC["registry_discover(req)"]
  DISC --> LOAD1["_load_registry()"]
  LOAD1 --> FILE1["agent_registry.json"]
  DISC --> DISC_OK["200 JSON list of agents"]

  %% registry register
  R3 --> REG["registry_register(req)"]
  REG --> KEY{"x-admin-key valid?"}
  KEY -- No --> REG_401["401 Unauthorized"]
  KEY -- Yes --> JSON1{"Valid JSON?"}
  JSON1 -- No --> REG_400["400 Invalid JSON body"]
  JSON1 -- Yes --> FIELDS{"Has appid and name?"}
  FIELDS -- No --> REG_400B["400 Missing required fields"]
  FIELDS -- Yes --> LOAD2["_load_registry()"]
  LOAD2 --> FILE2["agent_registry.json"]
  LOAD2 --> SAVE["_save_registry(payload)"]
  SAVE --> FILE3["agent_registry.json updated"]
  REG --> REG_OK["200 OK (status=ok, agent=payload)"]

  %% chat
  R4 --> CHAT["chat(req)"]
  CHAT --> HDR{"Has x-agent-appid?"}
  HDR -- No --> CHAT_401["401 Missing x-agent-appid"]
  HDR -- Yes --> LOAD3["_load_registry()"]
  LOAD3 --> FILE4["agent_registry.json"]
  LOAD3 --> META{"Agent registered?"}
  META -- No --> CHAT_403A["403 Agent not registered"]
  META -- Yes --> BODY{"Valid JSON body?"}
  BODY -- No --> CHAT_400["400 Invalid JSON body"]
  BODY -- Yes --> MSG{"Has message?"}
  MSG -- No --> CHAT_400B["400 Missing message"]
  MSG -- Yes --> ROLE{"Has role agent.chat.invoke?"}
  ROLE -- No --> CHAT_403B["403 Missing required role"]
  ROLE -- Yes --> AOAI["Create AOAI client + read deployment env"]
  AOAI --> CALL["Azure OpenAI chat.completions.create()"]
  CALL --> RESP{"AOAI success?"}
  RESP -- No --> CHAT_502["502 AOAI call failed"]
  RESP -- Yes --> CHAT_OK["200 JSON (appid, roles, name, answer)"]

