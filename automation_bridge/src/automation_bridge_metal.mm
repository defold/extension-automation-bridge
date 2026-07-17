#include "automation_bridge_private.h"

#if defined(DM_DEBUG) && defined(DM_PLATFORM_OSX)

#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>

namespace dmAutomationBridge
{
    static void SetMetalCaptureError(const char* error)
    {
        dmSnPrintf(g_AutomationBridge.m_MetalCaptureError,
                   sizeof(g_AutomationBridge.m_MetalCaptureError),
                   "%s",
                   error ? error : "Metal capture failed");
        g_AutomationBridge.m_MetalCaptureState = METAL_CAPTURE_FAILED;
        g_AutomationBridge.m_MetalCaptureStopRequested = false;
    }

    bool IsMetalCaptureSupported()
    {
        if (!g_AutomationBridge.m_GraphicsContext ||
            dmGraphics::GetInstalledAdapterFamily() != dmGraphics::ADAPTER_FAMILY_METAL)
        {
            return false;
        }

        if (@available(macOS 10.15, *))
        {
            return [[MTLCaptureManager sharedCaptureManager] supportsDestination:MTLCaptureDestinationGPUTraceDocument];
        }
        return false;
    }

    bool ScheduleMetalCapture(const char* path, uint32_t frames)
    {
        if (g_AutomationBridge.m_MetalCaptureState == METAL_CAPTURE_PENDING ||
            g_AutomationBridge.m_MetalCaptureState == METAL_CAPTURE_CAPTURING)
        {
            return false;
        }

        int written = dmSnPrintf(g_AutomationBridge.m_MetalCapturePath,
                                 sizeof(g_AutomationBridge.m_MetalCapturePath),
                                 "%s",
                                 path);
        if (written < 0 || (uint32_t)written >= sizeof(g_AutomationBridge.m_MetalCapturePath))
        {
            return false;
        }

        g_AutomationBridge.m_MetalCaptureFrames = frames;
        g_AutomationBridge.m_MetalCaptureFramesCaptured = 0;
        g_AutomationBridge.m_MetalCaptureStopRequested = false;
        g_AutomationBridge.m_MetalCaptureError[0] = 0;
        g_AutomationBridge.m_MetalCaptureState = METAL_CAPTURE_PENDING;
        return true;
    }

    bool RequestMetalCaptureStop()
    {
        if (g_AutomationBridge.m_MetalCaptureState == METAL_CAPTURE_PENDING)
        {
            g_AutomationBridge.m_MetalCaptureState = METAL_CAPTURE_CANCELED;
            g_AutomationBridge.m_MetalCaptureStopRequested = false;
            return true;
        }
        if (g_AutomationBridge.m_MetalCaptureState != METAL_CAPTURE_CAPTURING)
        {
            return false;
        }

        g_AutomationBridge.m_MetalCaptureStopRequested = true;
        return true;
    }

    void ProcessPendingMetalCaptureStart()
    {
        if (g_AutomationBridge.m_MetalCaptureState != METAL_CAPTURE_PENDING)
        {
            return;
        }

        @autoreleasepool
        {
            NSWindow* window = (NSWindow*)dmGraphics::GetNativeOSXNSWindow();
            NSView* view = window ? [window contentView] : nil;
            CAMetalLayer* layer = view && [[view layer] isKindOfClass:[CAMetalLayer class]]
                ? (CAMetalLayer*)[view layer]
                : nil;
            id<MTLDevice> device = layer ? [layer device] : nil;
            if (!device)
            {
                SetMetalCaptureError("Defold Metal layer device is unavailable");
                return;
            }

            MTLCaptureDescriptor* descriptor = [[MTLCaptureDescriptor alloc] init];
            descriptor.captureObject = device;
            descriptor.destination = MTLCaptureDestinationGPUTraceDocument;
            descriptor.outputURL = [NSURL fileURLWithPath:[NSString stringWithUTF8String:g_AutomationBridge.m_MetalCapturePath]];

            NSError* error = nil;
            BOOL started = [[MTLCaptureManager sharedCaptureManager] startCaptureWithDescriptor:descriptor error:&error];
            [descriptor release];

            if (!started)
            {
                const char* message = error ? [[error localizedDescription] UTF8String] : "Metal capture could not be started";
                SetMetalCaptureError(message);
                return;
            }

            g_AutomationBridge.m_MetalCaptureState = METAL_CAPTURE_CAPTURING;
            dmLogInfo("Automation Bridge Metal capture started: '%s' (%u frame%s)",
                      g_AutomationBridge.m_MetalCapturePath,
                      g_AutomationBridge.m_MetalCaptureFrames,
                      g_AutomationBridge.m_MetalCaptureFrames == 1 ? "" : "s");
        }
    }

    void ProcessMetalCapturePostRender()
    {
        if (g_AutomationBridge.m_MetalCaptureState != METAL_CAPTURE_CAPTURING)
        {
            return;
        }

        ++g_AutomationBridge.m_MetalCaptureFramesCaptured;
        if (!g_AutomationBridge.m_MetalCaptureStopRequested &&
            g_AutomationBridge.m_MetalCaptureFramesCaptured < g_AutomationBridge.m_MetalCaptureFrames)
        {
            return;
        }

        [[MTLCaptureManager sharedCaptureManager] stopCapture];
        g_AutomationBridge.m_MetalCaptureState = METAL_CAPTURE_COMPLETE;
        g_AutomationBridge.m_MetalCaptureStopRequested = false;
        dmLogInfo("Automation Bridge Metal capture completed: '%s'", g_AutomationBridge.m_MetalCapturePath);
    }

    void StopMetalCapture()
    {
        if (g_AutomationBridge.m_MetalCaptureState == METAL_CAPTURE_CAPTURING)
        {
            [[MTLCaptureManager sharedCaptureManager] stopCapture];
        }
        if (g_AutomationBridge.m_MetalCaptureState == METAL_CAPTURE_PENDING ||
            g_AutomationBridge.m_MetalCaptureState == METAL_CAPTURE_CAPTURING)
        {
            g_AutomationBridge.m_MetalCaptureState = METAL_CAPTURE_COMPLETE;
        }
        g_AutomationBridge.m_MetalCaptureStopRequested = false;
    }
}

#endif
