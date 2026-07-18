#include "automation_bridge_recording.h"

#if !defined(DM_PLATFORM_OSX) && !defined(DM_PLATFORM_WINDOWS)

#include <string.h>

namespace dmAutomationBridge
{
    static void UnsupportedStatus(RecordingStatus* status)
    {
        memset(status, 0, sizeof(*status));
        status->m_Supported = false;
        strcpy(status->m_Failure, NativeRecordingUnsupportedReason());
    }

    bool IsNativeRecordingSupported()
    {
        return false;
    }

    bool IsNativeRecordingAudioSupported()
    {
        return false;
    }

    const char* NativeRecordingBackendName()
    {
        return "unsupported";
    }

    const char* NativeRecordingMinimumPlatformVersion()
    {
        return "";
    }

    const char* NativeRecordingUnsupportedReason()
    {
        return "native video recording is available on supported macOS and Windows desktop runtimes";
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
