#include "automation_bridge_private.h"

#if defined(DM_DEBUG) && !defined(DM_PLATFORM_OSX)

namespace dmAutomationBridge
{
    bool IsMetalCaptureSupported()
    {
        return false;
    }

    bool ScheduleMetalCapture(const char* path, uint32_t frames)
    {
        (void)path;
        (void)frames;
        return false;
    }

    bool RequestMetalCaptureStop()
    {
        return false;
    }

    void ProcessPendingMetalCaptureStart()
    {
    }

    void ProcessMetalCapturePostRender()
    {
    }

    void StopMetalCapture()
    {
    }
}

#endif
