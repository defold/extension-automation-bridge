#include "automation_bridge_recording.h"

#if !defined(DM_PLATFORM_OSX)

#include <string.h>

namespace dmAutomationBridge
{
    static void UnsupportedStatus(RecordingStatus* status)
    {
        memset(status, 0, sizeof(*status));
        status->m_Supported = false;
        strcpy(status->m_Failure, "native video recording is available on macOS 15 or newer");
    }

    bool IsNativeRecordingSupported()
    {
        return false;
    }

    void GetNativeRecordingStatus(RecordingStatus* status)
    {
        UnsupportedStatus(status);
    }

    bool StartNativeRecording(const char*, uint32_t, uint32_t, uint32_t, bool, RecordingStatus* status)
    {
        UnsupportedStatus(status);
        return false;
    }

    bool StopNativeRecording(RecordingStatus* status)
    {
        UnsupportedStatus(status);
        return false;
    }

    void FinalizeNativeRecording()
    {
    }
}

#endif
