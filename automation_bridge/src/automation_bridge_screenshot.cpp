#include "automation_bridge_private.h"
#include "fpng/fpng.h"

#if defined(DM_DEBUG)

#include <errno.h>
#include <dmsdk/dlib/crypt.h>
#include <dmsdk/dlib/sys.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <vector>

#if defined(_WIN32)
#include <direct.h>
#endif

namespace dmAutomationBridge
{
    bool IsScreenshotSupported()
    {
        if (!g_AutomationBridge.m_GraphicsContext)
        {
            return false;
        }

        dmGraphics::AdapterFamily family = dmGraphics::GetInstalledAdapterFamily();
        return family == dmGraphics::ADAPTER_FAMILY_OPENGL ||
               family == dmGraphics::ADAPTER_FAMILY_OPENGLES ||
               family == dmGraphics::ADAPTER_FAMILY_VULKAN;
    }


    static void NormalizeSlashes(char* path)
    {
        while (*path)
        {
            if (*path == '\\')
            {
                *path = '/';
            }
            ++path;
        }
    }

    static const char* GetTempDirectory()
    {
#if defined(_WIN32)
        const char* temp_dir = getenv("TEMP");
        if (!temp_dir || !temp_dir[0])
        {
            temp_dir = getenv("TMP");
        }
#else
        const char* temp_dir = getenv("TMPDIR");
#endif
        if (!temp_dir || !temp_dir[0])
        {
#if defined(_WIN32)
            temp_dir = ".";
#else
            temp_dir = "/tmp";
#endif
        }
        return temp_dir;
    }

    static bool EnsureScreenshotDirectory(char* directory, uint32_t directory_size)
    {
        const char* temp_dir = GetTempDirectory();
        size_t temp_dir_len = strlen(temp_dir);
        const char* separator = temp_dir_len > 0 && (temp_dir[temp_dir_len - 1] == '/' || temp_dir[temp_dir_len - 1] == '\\') ? "" : "/";
        int written = dmSnPrintf(directory, directory_size, "%s%sautomation-bridge", temp_dir, separator);
        if (written < 0 || (uint32_t)written >= directory_size)
        {
            return false;
        }
        NormalizeSlashes(directory);

#if defined(_WIN32)
        int result = _mkdir(directory);
#else
        int result = mkdir(directory, 0700);
#endif
        if (result != 0 && errno != EEXIST)
        {
            dmLogError("Unable to create Automation Bridge screenshot directory '%s' (%d)", directory, errno);
            return false;
        }
        return true;
    }

    bool BuildScreenshotPath(uint64_t capture_id, char* path, uint32_t path_size)
    {
        char directory[1024];
        if (!EnsureScreenshotDirectory(directory, sizeof(directory)))
        {
            return false;
        }

        uint32_t timestamp = (uint32_t)(dmTime::GetTime() / 1000000);
        int written = dmSnPrintf(path, path_size, "%s/screenshot-%u-%llu.png", directory, timestamp, (unsigned long long)capture_id);
        if (written < 0 || (uint32_t)written >= path_size)
        {
            return false;
        }
        NormalizeSlashes(path);
        return true;
    }

    bool ScheduleScreenshot(uint32_t after_frames, ScreenshotCapture* capture)
    {
        if (g_AutomationBridge.m_Screenshot.m_State == SCREENSHOT_PENDING)
        {
            return false;
        }
        if (g_AutomationBridge.m_Screenshot.m_State != SCREENSHOT_NONE)
        {
            if (g_AutomationBridge.m_ScreenshotHistory.m_Count >= 16)
            {
                ArrayErase(&g_AutomationBridge.m_ScreenshotHistory, 0);
            }
            ArrayPush(&g_AutomationBridge.m_ScreenshotHistory, &g_AutomationBridge.m_Screenshot);
        }

        ScreenshotCapture next;
        memset(&next, 0, sizeof(next));
        next.m_Id = ++g_AutomationBridge.m_ScreenshotCounter;
        next.m_State = SCREENSHOT_PENDING;
        next.m_AfterFrames = after_frames;
        next.m_Frame = g_AutomationBridge.m_Frame;
        next.m_SceneSequence = g_AutomationBridge.m_Snapshot.m_Sequence;
        next.m_Width = g_AutomationBridge.m_Snapshot.m_ViewportWidth;
        next.m_Height = g_AutomationBridge.m_Snapshot.m_ViewportHeight;
        if (!BuildScreenshotPath(next.m_Id, next.m_Path, sizeof(next.m_Path)))
        {
            return false;
        }
        g_AutomationBridge.m_Screenshot = next;
        if (capture)
        {
            *capture = next;
        }
        return true;
    }

    const ScreenshotCapture* FindScreenshotCapture(uint64_t capture_id)
    {
        if (g_AutomationBridge.m_Screenshot.m_Id == capture_id &&
            g_AutomationBridge.m_Screenshot.m_State != SCREENSHOT_NONE)
        {
            return &g_AutomationBridge.m_Screenshot;
        }
        for (uint32_t i = 0; i < g_AutomationBridge.m_ScreenshotHistory.m_Count; ++i)
        {
            const ScreenshotCapture* capture = &g_AutomationBridge.m_ScreenshotHistory.m_Data[i];
            if (capture->m_Id == capture_id)
            {
                return capture;
            }
        }
        return 0;
    }

    static bool ByteArrayPushBytes(ByteArray* array, const uint8_t* data, uint32_t count)
    {
        if (count == 0)
        {
            return true;
        }
        if (!ArrayReserve(array, array->m_Count + count))
        {
            return false;
        }
        memcpy(array->m_Data + array->m_Count, data, count);
        array->m_Count += count;
        return true;
    }

    static bool EncodePngRgba(const ByteArray* rgba, uint32_t width, uint32_t height, ByteArray* png)
    {
        uint64_t rgba_size = (uint64_t)width * height * 4;
        if (width == 0 || height == 0 || rgba_size != rgba->m_Count)
        {
            return false;
        }

        static bool initialized = false;
        if (!initialized)
        {
            fpng::fpng_init();
            initialized = true;
        }

        std::vector<uint8_t> encoded;
        if (!fpng::fpng_encode_image_to_memory(rgba->m_Data, width, height, 4, encoded) ||
            encoded.size() > UINT32_MAX)
        {
            return false;
        }

        png->m_Count = 0;
        return ByteArrayPushBytes(png, encoded.data(), (uint32_t)encoded.size());
    }

    static bool CaptureScreenshotPng(ByteArray* png)
    {
        if (!IsScreenshotSupported())
        {
            return false;
        }

        int32_t x = 0;
        int32_t y = 0;
        uint32_t width = 0;
        uint32_t height = 0;
        dmGraphics::GetViewport(g_AutomationBridge.m_GraphicsContext, &x, &y, &width, &height);
        if (width == 0 || height == 0)
        {
            return false;
        }

        ByteArray pixels;
        ArrayInit(&pixels);
        if (!ArraySetCount(&pixels, width * height * 4))
        {
            return false;
        }

        dmGraphics::ReadPixels(g_AutomationBridge.m_GraphicsContext, x, y, width, height, pixels.m_Data, pixels.m_Count);

        for (uint32_t i = 0; i < pixels.m_Count; i += 4)
        {
            uint8_t b = pixels.m_Data[i + 0];
            pixels.m_Data[i + 0] = pixels.m_Data[i + 2];
            pixels.m_Data[i + 2] = b;
        }

        // Defold's graphics adapters normalize ReadPixels rows to top-left
        // display order. Preserve that order so PNG pixels use the same
        // coordinates as HID, scene bounds, and coordinate conversion.
        bool ok = EncodePngRgba(&pixels, width, height, png);
        ArrayFree(&pixels);
        return ok;
    }

    static bool WriteBytesToFile(const char* path, const ByteArray* bytes)
    {
        FILE* file = 0;
#if defined(_WIN32)
        if (fopen_s(&file, path, "wb") != 0)
        {
            file = 0;
        }
#else
        file = fopen(path, "wb");
#endif
        if (!file)
        {
            return false;
        }
        size_t written = fwrite(bytes->m_Data, 1, bytes->m_Count, file);
        bool close_ok = fclose(file) == 0;
        return written == bytes->m_Count && close_ok;
    }

    static void SetScreenshotFailure(ScreenshotCapture* capture, const char* failure)
    {
        capture->m_State = SCREENSHOT_FAILED;
        dmSnPrintf(capture->m_Failure, sizeof(capture->m_Failure), "%s", failure ? failure : "capture failed");
    }

    static void FormatSha256(const ByteArray* bytes, char digest_string[65])
    {
        uint8_t digest[32];
        dmCrypt::HashSha256(bytes->m_Data, bytes->m_Count, digest);
        for (uint32_t i = 0; i < sizeof(digest); ++i)
        {
            dmSnPrintf(digest_string + i * 2, 3, "%02x", digest[i]);
        }
        digest_string[64] = 0;
    }

    void AppendScreenshotJson(StringBuffer* out, const ScreenshotCapture* capture)
    {
        const char* state = "none";
        if (capture->m_State == SCREENSHOT_PENDING) state = "pending";
        else if (capture->m_State == SCREENSHOT_COMPLETE) state = "complete";
        else if (capture->m_State == SCREENSHOT_FAILED) state = "failed";

        StringBufferAppend(out, "{\"capture_id\":");
        AppendNumber(out, (double)capture->m_Id);
        StringBufferAppend(out, ",\"state\":");
        AppendJsonString(out, state);
        StringBufferAppend(out, ",\"pending\":");
        StringBufferAppend(out, capture->m_State == SCREENSHOT_PENDING ? "true" : "false");
        StringBufferAppend(out, ",\"path\":");
        AppendJsonString(out, capture->m_Path);
        StringBufferAppend(out, ",\"format\":\"png\",\"engine_frame\":");
        AppendNumber(out, (double)capture->m_Frame);
        StringBufferAppend(out, ",\"scene_sequence\":");
        AppendNumber(out, (double)capture->m_SceneSequence);
        StringBufferAppend(out, ",\"width\":");
        AppendNumber(out, capture->m_Width);
        StringBufferAppend(out, ",\"height\":");
        AppendNumber(out, capture->m_Height);
        StringBufferAppend(out, ",\"sha256\":");
        if (capture->m_Sha256[0]) AppendJsonString(out, capture->m_Sha256); else StringBufferAppend(out, "null");
        StringBufferAppend(out, ",\"failure_reason\":");
        if (capture->m_Failure[0]) AppendJsonString(out, capture->m_Failure); else StringBufferAppend(out, "null");
        StringBufferAppendChar(out, '}');
    }

    void ProcessPendingScreenshot()
    {
        ScreenshotCapture* capture = &g_AutomationBridge.m_Screenshot;
        if (capture->m_State != SCREENSHOT_PENDING)
        {
            return;
        }
        if (capture->m_AfterFrames > 0)
        {
            --capture->m_AfterFrames;
            return;
        }

        UpdateSnapshot();
        g_AutomationBridge.m_SnapshotFrame = g_AutomationBridge.m_Frame;
        capture->m_Frame = g_AutomationBridge.m_Frame;
        capture->m_SceneSequence = g_AutomationBridge.m_Snapshot.m_Sequence;
        capture->m_Width = g_AutomationBridge.m_Snapshot.m_ViewportWidth;
        capture->m_Height = g_AutomationBridge.m_Snapshot.m_ViewportHeight;

        ByteArray png;
        ArrayInit(&png);
        if (!CaptureScreenshotPng(&png))
        {
            dmLogError("Unable to capture Automation Bridge screenshot");
            SetScreenshotFailure(capture, "capture_failed");
            ArrayFree(&png);
            return;
        }
        char temporary_path[1100];
        int temporary_written = dmSnPrintf(temporary_path, sizeof(temporary_path), "%s.tmp", capture->m_Path);
        if (temporary_written < 0 || (uint32_t)temporary_written >= sizeof(temporary_path) || !WriteBytesToFile(temporary_path, &png))
        {
            dmLogError("Unable to write Automation Bridge screenshot '%s'", capture->m_Path);
            SetScreenshotFailure(capture, "write_failed");
            ArrayFree(&png);
            return;
        }
        FormatSha256(&png, capture->m_Sha256);
        if (dmSys::Rename(capture->m_Path, temporary_path) != dmSys::RESULT_OK)
        {
            dmLogError("Unable to publish Automation Bridge screenshot '%s'", capture->m_Path);
            SetScreenshotFailure(capture, "atomic_rename_failed");
            ArrayFree(&png);
            return;
        }
        capture->m_State = SCREENSHOT_COMPLETE;
        ArrayFree(&png);
    }


}

#endif
