// Debug-only runtime inspection and input API for automation-driven Defold testing.
#if !defined(DM_RELEASE) && !defined(DM_HEADLESS) && !defined(DM_DEBUG)
#define DM_DEBUG
#endif

#include <dmsdk/sdk.h>

#if defined(DM_PLATFORM_WINDOWS)
#include <windows.h>
#elif defined(DM_PLATFORM_OSX) || defined(DM_PLATFORM_LINUX) || defined(DM_PLATFORM_ANDROID) || defined(DM_PLATFORM_IOS)
#include <unistd.h>
#endif

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
        for (uint32_t i = 0; i < bridge->m_InputHistory.m_Count; ++i)
        {
            FreeInputReceipt(&bridge->m_InputHistory.m_Data[i]);
        }
        ArrayFree(&bridge->m_InputHistory);
        FreeString(&bridge->m_ControllerClientId);
        FreeString(&bridge->m_ControllerSessionId);
        FreeApplicationBridge();
        ArrayFree(&bridge->m_ScreenshotHistory);
    }

    static void InitAutomationBridgeContext(AutomationBridgeContext* bridge)
    {
        memset(bridge, 0, sizeof(*bridge));
        InitSnapshot(&bridge->m_Snapshot);
        bridge->m_NextInputId = 1;
        bridge->m_DefaultInputDevice = INPUT_DEVICE_AUTO;
        bridge->m_DefaultInputVisualize = true;
        bridge->m_DisplayWidth = 960;
        bridge->m_DisplayHeight = 640;
        bridge->m_ProcessId = -1;
    }

    static int64_t GetPortableProcessId()
    {
#if defined(DM_PLATFORM_WINDOWS)
        return (int64_t)GetCurrentProcessId();
#elif defined(DM_PLATFORM_OSX) || defined(DM_PLATFORM_LINUX) || defined(DM_PLATFORM_ANDROID) || defined(DM_PLATFORM_IOS)
        return (int64_t)getpid();
#else
        return -1;
#endif
    }

    static void FormatIdentity(char* output, uint32_t output_size, const char* prefix, uint64_t hash)
    {
        dmSnPrintf(output, output_size, "%s:%016llx", prefix, (unsigned long long)hash);
    }

    static void InitializeIdentity(dmExtension::AppParams* params)
    {
        AutomationBridgeContext* bridge = &g_AutomationBridge;
        bridge->m_StartWallTime = dmTime::GetTime();
        bridge->m_StartMonotonicTime = dmTime::GetMonotonicTime();
        bridge->m_ProcessId = GetPortableProcessId();

        char instance_seed[128];
        dmSnPrintf(instance_seed, sizeof(instance_seed), "%llu:%llu:%p",
                   (unsigned long long)bridge->m_StartWallTime,
                   (unsigned long long)bridge->m_StartMonotonicTime,
                   (void*)bridge);
        FormatIdentity(bridge->m_EngineInstanceId, sizeof(bridge->m_EngineInstanceId), "engine", dmHashString64(instance_seed));

        const char* title = "";
        const char* version = "";
        const char* bootstrap = "";
        if (params->m_ConfigFile)
        {
            title = dmConfigFile::GetString(params->m_ConfigFile, "project.title", "");
            version = dmConfigFile::GetString(params->m_ConfigFile, "project.version", "");
            bootstrap = dmConfigFile::GetString(params->m_ConfigFile, "bootstrap.main_collection", "");
        }
        FormatIdentity(bridge->m_ProjectIdentity, sizeof(bridge->m_ProjectIdentity), "project", dmHashString64(title));

        char build_seed[1024];
        dmSnPrintf(build_seed, sizeof(build_seed), "%s\n%s\n%s", title, version, bootstrap);
        FormatIdentity(bridge->m_BuildIdentity, sizeof(bridge->m_BuildIdentity), "config", dmHashString64(build_seed));
    }

    static ExtensionResult PreRender(ExtensionParams* params)
    {
        DefoldPrivateApiInitialize(params);
        return EXTENSION_RESULT_OK;
    }

    static ExtensionResult PostRender(ExtensionParams* params)
    {
        DefoldPrivateApiInitialize(params);
        DefoldPrivateApiDrawInputVisualization(&g_AutomationBridge.m_InputVisualization);
        ProcessPendingScreenshot();
        return EXTENSION_RESULT_OK;
    }

    static dmExtension::Result AppInitialize(dmExtension::AppParams* params)
    {
        FreeAutomationBridgeContext(&g_AutomationBridge);
        InitAutomationBridgeContext(&g_AutomationBridge);
        InitializeIdentity(params);
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
        int32_t event_capacity = params->m_ConfigFile ? dmConfigFile::GetInt(params->m_ConfigFile, "automation_bridge.event_capacity", 256) : 256;
        event_capacity = event_capacity < 16 ? 16 : (event_capacity > 4096 ? 4096 : event_capacity);
        bool application_api_enabled = params->m_ConfigFile && dmConfigFile::GetInt(params->m_ConfigFile, "automation_bridge.application_api", 0) == 1;
        InitApplicationBridge((uint32_t)event_capacity, application_api_enabled);
        g_AutomationBridge.m_Initialized = true;
        RegisterWebEndpoint(params);
        dmExtension::RegisterCallback(dmExtension::CALLBACK_PRE_RENDER, PreRender);
        dmExtension::RegisterCallback(dmExtension::CALLBACK_POST_RENDER, PostRender);
        return dmExtension::RESULT_OK;
    }

    static dmExtension::Result Initialize(dmExtension::Params* params)
    {
        g_AutomationBridge.m_GraphicsContext = (dmGraphics::HContext)ExtensionParamsGetContextByName((ExtensionParams*)params, "graphics");
        DefoldPrivateApiInitialize(params);
        RegisterApplicationLua(params->m_L);
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
        FinalizeApplicationLua();
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
        UpdateApplicationBridge();

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
