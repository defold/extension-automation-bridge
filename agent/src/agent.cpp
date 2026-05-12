// Debug-only runtime inspection and input API for agent-driven Defold testing.
#if !defined(DM_RELEASE) && !defined(DM_HEADLESS) && !defined(DM_DEBUG)
#define DM_DEBUG
#endif

#include <dmsdk/sdk.h>

#define MODULE_NAME Agent
#define LIB_NAME "agent"

#if defined(DM_DEBUG)

#include "agent_private.h"

namespace dmAgent
{
    AgentContext g_Agent;
    static void FreeAgentContext(AgentContext* agent)
    {
        FreeSnapshot(&agent->m_Snapshot);
        for (uint32_t i = 0; i < agent->m_InputEvents.m_Count; ++i)
        {
            FreeInputEvent(&agent->m_InputEvents.m_Data[i]);
        }
        ArrayFree(&agent->m_InputEvents);
    }

    static void InitAgentContext(AgentContext* agent)
    {
        memset(agent, 0, sizeof(*agent));
        InitSnapshot(&agent->m_Snapshot);
    }

    static ExtensionResult PostRender(ExtensionParams* params)
    {
        (void)params;
        ProcessPendingScreenshot();
        return EXTENSION_RESULT_OK;
    }

    static dmExtension::Result AppInitialize(dmExtension::AppParams* params)
    {
        FreeAgentContext(&g_Agent);
        InitAgentContext(&g_Agent);
        g_Agent.m_Register = dmEngine::GetGameObjectRegister(params);
        g_Agent.m_HidContext = dmEngine::GetHIDContext(params);
        g_Agent.m_LastTime = dmTime::GetTime();
        g_Agent.m_Initialized = true;
        RegisterWebEndpoint(params);
        dmExtension::RegisterCallback(dmExtension::CALLBACK_POST_RENDER, PostRender);
        return dmExtension::RESULT_OK;
    }

    static dmExtension::Result Initialize(dmExtension::Params* params)
    {
        g_Agent.m_GraphicsContext = (dmGraphics::HContext)ExtensionParamsGetContextByName((ExtensionParams*)params, "graphics");
        dmLogInfo("Registered %s extension", LIB_NAME);
        return dmExtension::RESULT_OK;
    }

    static dmExtension::Result AppFinalize(dmExtension::AppParams* params)
    {
        (void)params;
        UnregisterWebEndpoint();
        FreeAgentContext(&g_Agent);
        InitAgentContext(&g_Agent);
        return dmExtension::RESULT_OK;
    }

    static dmExtension::Result Finalize(dmExtension::Params* params)
    {
        (void)params;
        return dmExtension::RESULT_OK;
    }

    static dmExtension::Result OnUpdate(dmExtension::Params* params)
    {
        (void)params;
        if (!g_Agent.m_Initialized)
        {
            return dmExtension::RESULT_OK;
        }

        uint64_t time = dmTime::GetTime();
        float dt = (float)((time - g_Agent.m_LastTime) / 1000000.0);
        g_Agent.m_LastTime = time;

        UpdateInput(dt);

        return dmExtension::RESULT_OK;
    }

}

DM_DECLARE_EXTENSION(MODULE_NAME, LIB_NAME, dmAgent::AppInitialize, dmAgent::AppFinalize, dmAgent::Initialize, dmAgent::OnUpdate, 0, dmAgent::Finalize)

#else

static dmExtension::Result InitializeAgent(dmExtension::Params* params)
{
    (void)params;
    dmLogInfo("Registered %s extension (null)", LIB_NAME);
    return dmExtension::RESULT_OK;
}

static dmExtension::Result FinalizeAgent(dmExtension::Params* params)
{
    (void)params;
    return dmExtension::RESULT_OK;
}

DM_DECLARE_EXTENSION(MODULE_NAME, LIB_NAME, 0, 0, InitializeAgent, 0, 0, FinalizeAgent)

#endif

#undef LIB_NAME
