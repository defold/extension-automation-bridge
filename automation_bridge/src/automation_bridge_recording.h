#pragma once

#include <stdint.h>

namespace dmAutomationBridge
{
    struct RecordingStatus
    {
        bool     m_Supported;
        bool     m_Active;
        bool     m_Finalized;
        bool     m_Audio;
        uint32_t m_Width;
        uint32_t m_Height;
        uint32_t m_Fps;
        uint64_t m_StartedWallTime;
        uint64_t m_StartedMonotonicTime;
        uint64_t m_StoppedWallTime;
        uint64_t m_StoppedMonotonicTime;
        char     m_Path[1024];
        char     m_Failure[512];
    };

    bool IsNativeRecordingSupported();
    void GetNativeRecordingStatus(RecordingStatus* status);
    bool StartNativeRecording(const char* path, uint32_t width, uint32_t height,
                              uint32_t fps, bool audio, RecordingStatus* status);
    bool StopNativeRecording(RecordingStatus* status);
    void FinalizeNativeRecording();
}
