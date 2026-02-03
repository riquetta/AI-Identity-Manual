``` mermaid

sequenceDiagram
  autonumber
  participant Client as Client (Postman/Web/Service)
  participant Func as Azure Functions (FunctionApp)
  participant Reg as Registry File (agent_registry.json)
  participant AOAI as Azure OpenAI

  rect rgb(245,245,245)
    note over Client,Func: Discover agents: GET /api/registry/discover
    Client->>Func: GET /api/registry/discover
    Func->>Reg: _load_registry()
    Reg-->>Func: {"agents": {...}}
    Func-->>Client: 200 {"agents":[...]}
  end

  rect rgb(245,245,245)
    note over Client,Func: Register agent (admin): POST /api/registry/register
    Client->>Func: POST /api/registry/register (x-admin-key)
    alt invalid x-admin-key
      Func-->>Client: 401 Unauthorized
    else valid x-admin-key
      Func->>Reg: _load_registry()
      Func->>Reg: _save_registry(payload under agents[appid])
      Func-->>Client: 200 {"status":"ok","agent":payload}
    end
  end

  rect rgb(245,245,245)
    note over Client,AOAI: Chat: POST /api/chat (headers expected from APIM)
    Client->>Func: POST /api/chat<br/>Headers: x-agent-appid, x-agent-roles<br/>Body: {"message":"..."}
    alt missing x-agent-appid
      Func-->>Client: 401 Missing x-agent-appid
    else has x-agent-appid
      Func->>Reg: _load_registry()
      alt agent not in registry
        Func-->>Client: 403 Agent not registered
      else agent registered
        alt missing role agent.chat.invoke
          Func-->>Client: 403 Missing required role
        else role ok
          Func->>AOAI: chat.completions.create(model=deployment, messages=[system,user])
          alt AOAI error
            Func-->>Client: 502 Azure OpenAI call failed
          else success
            AOAI-->>Func: completion
            Func-->>Client: 200 {agent_appid, agent_roles, agent_name, answer}
          end
        end
      end
    end
  end
