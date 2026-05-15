// Debug-only runtime inspection and input API for automation-driven Defold testing.
#if !defined(DM_RELEASE) && !defined(DM_HEADLESS) && !defined(DM_DEBUG)
#define DM_DEBUG
#endif

#include <dmsdk/sdk.h>

#define MODULE_NAME AutomationBridge
#define LIB_NAME "automation_bridge"

#if defined(DM_DEBUG)

#include "automation_bridge_private.h"
#include "automation_bridge_defold_private_api.h"

namespace dmAutomationBridge
{
    AutomationBridgeContext g_AutomationBridge;
    static void FreeAutomationBridgeContext(AutomationBridgeContext* bridge)
    {
        FreeSnapshot(&bridge->m_Snapshot);
        for (uint32_t i = 0; i < bridge->m_InputEvents.m_Count; ++i)
        {
            FreeInputEvent(&bridge->m_InputEvents.m_Data[i]);
        }
        ArrayFree(&bridge->m_InputEvents);
    }

    static void InitAutomationBridgeContext(AutomationBridgeContext* bridge)
    {
        memset(bridge, 0, sizeof(*bridge));
        InitSnapshot(&bridge->m_Snapshot);
        bridge->m_DisplayWidth = 960;
        bridge->m_DisplayHeight = 640;
    }

    static ExtensionResult PostRender(ExtensionParams* params)
    {
        (void)params;
        ProcessPendingScreenshot();
        return EXTENSION_RESULT_OK;
    }

    static dmExtension::Result AppInitialize(dmExtension::AppParams* params)
    {
        FreeAutomationBridgeContext(&g_AutomationBridge);
        InitAutomationBridgeContext(&g_AutomationBridge);
        g_AutomationBridge.m_Register = dmEngine::GetGameObjectRegister(params);
        g_AutomationBridge.m_HidContext = dmEngine::GetHIDContext(params);
        if (params->m_ConfigFile)
        {
            int32_t display_width = dmConfigFile::GetInt(params->m_ConfigFile, "display.width", 960);
            int32_t display_height = dmConfigFile::GetInt(params->m_ConfigFile, "display.height", 640);
            if (display_width > 0)
            {
                g_AutomationBridge.m_DisplayWidth = (uint32_t)display_width;
            }
            if (display_height > 0)
            {
                g_AutomationBridge.m_DisplayHeight = (uint32_t)display_height;
            }
        }
        g_AutomationBridge.m_LastTime = dmTime::GetTime();
        g_AutomationBridge.m_Initialized = true;
        RegisterWebEndpoint(params);
        dmExtension::RegisterCallback(dmExtension::CALLBACK_POST_RENDER, PostRender);
        return dmExtension::RESULT_OK;
    }

    static dmExtension::Result Initialize(dmExtension::Params* params)
    {
        g_AutomationBridge.m_GraphicsContext = (dmGraphics::HContext)ExtensionParamsGetContextByName((ExtensionParams*)params, "graphics");
        DefoldPrivateApiInitialize(params);
        dmLogInfo("Registered %s extension", LIB_NAME);
        return dmExtension::RESULT_OK;
    }

    static dmExtension::Result AppFinalize(dmExtension::AppParams* params)
    {
        (void)params;
        UnregisterWebEndpoint();
        FreeAutomationBridgeContext(&g_AutomationBridge);
        InitAutomationBridgeContext(&g_AutomationBridge);
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
        if (!g_AutomationBridge.m_Initialized)
        {
            return dmExtension::RESULT_OK;
        }

        uint64_t time = dmTime::GetTime();
        float dt = (float)((time - g_AutomationBridge.m_LastTime) / 1000000.0);
        g_AutomationBridge.m_LastTime = time;
        ++g_AutomationBridge.m_Frame;

        UpdateInput(dt);

        return dmExtension::RESULT_OK;
    }

}

DM_DECLARE_EXTENSION(MODULE_NAME, LIB_NAME, dmAutomationBridge::AppInitialize, dmAutomationBridge::AppFinalize, dmAutomationBridge::Initialize, dmAutomationBridge::OnUpdate, 0, dmAutomationBridge::Finalize)

#else

static dmExtension::Result InitializeAutomationBridge(dmExtension::Params* params)
{
    (void)params;
    dmLogInfo("Registered %s extension (null)", LIB_NAME);
    return dmExtension::RESULT_OK;
}

static dmExtension::Result FinalizeAutomationBridge(dmExtension::Params* params)
{
    (void)params;
    return dmExtension::RESULT_OK;
}

DM_DECLARE_EXTENSION(MODULE_NAME, LIB_NAME, 0, 0, InitializeAutomationBridge, 0, 0, FinalizeAutomationBridge)

#endif

#undef LIB_NAME
