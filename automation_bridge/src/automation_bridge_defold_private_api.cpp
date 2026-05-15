#include "automation_bridge_defold_private_api.h"

#if defined(DM_DEBUG)

#include <dmsdk/gui/gui.h>
#include <dmsdk/platform/window.h>
#include <dmsdk/render/render.h>

#include <float.h>
#include <math.h>
#include <string.h>

// Defold private/internal API declarations.
//
// These functions/structures exist in the engine but are not exposed as stable
// extension SDK APIs. The declarations are kept local to this adapter so
// private-engine coupling is explicit and does not leak into bridge code.
namespace dmGui
{
    dmVMath::Matrix4 GetNodeWorldTransform(HScene scene, HNode node);
}

namespace dmGraphics
{
    HWindow GetWindow(HContext context);
}

namespace dmPlatform
{
    float GetDisplayScaleFactor(HWindow window);
    void SetWindowSize(HWindow window, uint32_t width, uint32_t height);
}

namespace dmRender
{
    typedef uint32_t HRenderCamera;

    struct FrustumOptions;
    void RenderListBegin(HRenderContext render_context);
    void RenderListEnd(HRenderContext render_context);
    void SetViewMatrix(HRenderContext render_context, const dmVMath::Matrix4& view);
    void SetProjectionMatrix(HRenderContext render_context, const dmVMath::Matrix4& projection);
    Result ClearRenderObjects(HRenderContext context);
    Result DrawDebug3d(HRenderContext context, const FrustumOptions* frustum_options);
    Result CameraWorldToScreen(HRenderContext render_context, HRenderCamera camera, const dmVMath::Vector3& world, dmVMath::Vector3* out_screen);
    void Line3D(HRenderContext context, dmVMath::Point3 start, dmVMath::Point3 end, dmVMath::Vector4 start_color, dmVMath::Vector4 end_color);
}

namespace dmAutomationBridge
{
    namespace
    {
        struct GuiComponentPrivateApi
        {
            void*         m_World;
            void*         m_Resource;
            dmGui::HScene m_Scene;
        };

        struct CameraComponentPrivateApi
        {
            dmGameObject::HInstance m_Instance;
            dmRender::HRenderCamera m_RenderCamera;
        };

        static dmRender::HRenderCamera g_SnapshotCamera = 0;

        static bool IsUsableScreen(const Snapshot* snapshot)
        {
            return snapshot && snapshot->m_WindowWidth > 0 && snapshot->m_WindowHeight > 0;
        }

        static bool IsUsablePoint(float x, float y)
        {
            return IsFiniteFloat(x) && IsFiniteFloat(y);
        }

        static void InitAccumulator(float* min_x, float* min_y, float* max_x, float* max_y)
        {
            *min_x = FLT_MAX;
            *min_y = FLT_MAX;
            *max_x = -FLT_MAX;
            *max_y = -FLT_MAX;
        }

        static bool AccumulatePoint(float x, float y, float* min_x, float* min_y, float* max_x, float* max_y)
        {
            if (!IsUsablePoint(x, y))
            {
                return false;
            }
            *min_x = MinFloat(*min_x, x);
            *min_y = MinFloat(*min_y, y);
            *max_x = MaxFloat(*max_x, x);
            *max_y = MaxFloat(*max_y, y);
            return true;
        }

        static bool SetBoundsFromAccumulator(float min_x, float min_y, float max_x, float max_y, const Snapshot* snapshot, Bounds* out_bounds)
        {
            if (min_x == FLT_MAX || min_y == FLT_MAX || max_x == -FLT_MAX || max_y == -FLT_MAX)
            {
                return false;
            }

            out_bounds->m_X = min_x;
            out_bounds->m_Y = min_y;
            out_bounds->m_W = MaxFloat(0.0f, max_x - min_x);
            out_bounds->m_H = MaxFloat(0.0f, max_y - min_y);
            out_bounds->m_CX = min_x + out_bounds->m_W * 0.5f;
            out_bounds->m_CY = min_y + out_bounds->m_H * 0.5f;
            out_bounds->m_NX = out_bounds->m_CX / (float)snapshot->m_WindowWidth;
            out_bounds->m_NY = out_bounds->m_CY / (float)snapshot->m_WindowHeight;
            out_bounds->m_Valid = true;
            return true;
        }

        static bool IsDesktopWindowResizeSupported()
        {
#if defined(DM_PLATFORM_OSX) || defined(DM_PLATFORM_WINDOWS) || defined(DM_PLATFORM_LINUX)
            return true;
#else
            return false;
#endif
        }

        static bool IsCloseDimension(uint32_t actual, uint32_t expected, float tolerance)
        {
            return fabsf((float)actual - (float)expected) <= tolerance;
        }

        static bool CurrentWindowSizeMatches(uint32_t width, uint32_t height, float scale)
        {
            if (!g_AutomationBridge.m_GraphicsContext)
            {
                return false;
            }

            uint32_t actual_width = dmGraphics::GetWindowWidth(g_AutomationBridge.m_GraphicsContext);
            uint32_t actual_height = dmGraphics::GetWindowHeight(g_AutomationBridge.m_GraphicsContext);
            float tolerance = MaxFloat(1.0f, ceilf(scale));
            return IsCloseDimension(actual_width, width, tolerance) &&
                   IsCloseDimension(actual_height, height, tolerance);
        }

        static bool ScreenToDebugWorld(float x, float y, dmVMath::Point3* out)
        {
            if (!g_AutomationBridge.m_GraphicsContext)
            {
                return false;
            }

            uint32_t window_height = dmGraphics::GetWindowHeight(g_AutomationBridge.m_GraphicsContext);
            if (window_height == 0)
            {
                return false;
            }

            *out = dmVMath::Point3(x, (float)window_height - y, 0.0f);
            return true;
        }

        static void DrawDebugLine(dmRender::HRenderContext render_context, float x1, float y1, float x2, float y2, const dmVMath::Vector4& color)
        {
            dmVMath::Point3 start;
            dmVMath::Point3 end;
            if (!ScreenToDebugWorld(x1, y1, &start) || !ScreenToDebugWorld(x2, y2, &end))
            {
                return;
            }

            dmRender::Line3D(render_context, start, end, color, color);
        }

        static void DrawDebugCircle(dmRender::HRenderContext render_context, float x, float y, float radius, const dmVMath::Vector4& color)
        {
            static const uint32_t SEGMENTS = 32;
            static const float TAU = 6.28318530717958647692f;

            float previous_x = x + radius;
            float previous_y = y;
            for (uint32_t i = 1; i <= SEGMENTS; ++i)
            {
                float angle = ((float)i / (float)SEGMENTS) * TAU;
                float next_x = x + cosf(angle) * radius;
                float next_y = y + sinf(angle) * radius;
                DrawDebugLine(render_context, previous_x, previous_y, next_x, next_y, color);
                previous_x = next_x;
                previous_y = next_y;
            }
        }

        static void AdvanceInputVisualization(InputVisualization* visualization)
        {
            uint64_t now = dmTime::GetTime();
            if (visualization->m_LastRenderTime != 0)
            {
                visualization->m_Age += (float)((now - visualization->m_LastRenderTime) / 1000000.0);
                if (visualization->m_Age >= visualization->m_Duration)
                {
                    memset(visualization, 0, sizeof(*visualization));
                    return;
                }
            }
            visualization->m_LastRenderTime = now;
        }

        static bool ComputeGuiBounds(const dmGameObject::SceneNode* scene_node, const Node* node, const Snapshot* snapshot, Bounds* out_bounds)
        {
            if (!scene_node || scene_node->m_Component == 0 || scene_node->m_Node == 0)
            {
                return false;
            }

            GuiComponentPrivateApi* component = (GuiComponentPrivateApi*)scene_node->m_Component;
            dmGui::HScene scene = component->m_Scene;
            dmGui::HNode gui_node = (dmGui::HNode)scene_node->m_Node;
            if (!scene || gui_node == dmGui::INVALID_HANDLE)
            {
                return false;
            }

            dmVMath::Matrix4 transform = dmGui::GetNodeWorldTransform(scene, gui_node);
            dmVMath::Vector4 size = dmGui::GetNodeProperty(scene, gui_node, dmGui::PROPERTY_SIZE);
            float width = fabsf(size.getX());
            float height = fabsf(size.getY());

            float left = -width * node->m_AnchorX;
            float right = left + width;
            float bottom = -height * node->m_AnchorY;
            float top = bottom + height;

            const dmVMath::Vector4 points[] = {
                dmVMath::Vector4(left, bottom, 0.0f, 1.0f),
                dmVMath::Vector4(right, bottom, 0.0f, 1.0f),
                dmVMath::Vector4(right, top, 0.0f, 1.0f),
                dmVMath::Vector4(left, top, 0.0f, 1.0f),
            };

            float min_x;
            float min_y;
            float max_x;
            float max_y;
            InitAccumulator(&min_x, &min_y, &max_x, &max_y);

            for (uint32_t i = 0; i < DM_ARRAY_SIZE(points); ++i)
            {
                dmVMath::Vector4 point = transform * points[i];
                float screen_x = point.getX();
                float screen_y = (float)snapshot->m_WindowHeight - point.getY();
                if (!AccumulatePoint(screen_x, screen_y, &min_x, &min_y, &max_x, &max_y))
                {
                    return false;
                }
            }

            return SetBoundsFromAccumulator(min_x, min_y, max_x, max_y, snapshot, out_bounds);
        }

        static bool AccumulateCameraPoint(const Snapshot* snapshot, const dmVMath::Vector3& world, float* min_x, float* min_y, float* max_x, float* max_y)
        {
            if (!g_AutomationBridge.m_RenderContext || g_SnapshotCamera == 0)
            {
                return false;
            }

            dmVMath::Vector3 screen;
            dmRender::Result result = dmRender::CameraWorldToScreen((dmRender::HRenderContext)g_AutomationBridge.m_RenderContext, g_SnapshotCamera, world, &screen);
            if (result != dmRender::RESULT_OK)
            {
                return false;
            }

            float screen_x = screen.getX();
            float screen_y = (float)snapshot->m_WindowHeight - screen.getY();
            return AccumulatePoint(screen_x, screen_y, min_x, min_y, max_x, max_y);
        }

        static bool ComputeCameraBounds(const Node* node, const Snapshot* snapshot, Bounds* out_bounds)
        {
            if (g_SnapshotCamera == 0 || !node->m_HasPosition)
            {
                return false;
            }

            float width = node->m_HasSize ? fabsf(node->m_Size[0]) : 0.0f;
            float height = node->m_HasSize ? fabsf(node->m_Size[1]) : 0.0f;
            float x = node->m_Position[0];
            float y = node->m_Position[1];
            float z = node->m_Position[2];

            if (!IsFiniteFloat(x) || !IsFiniteFloat(y) || !IsFiniteFloat(z) || !IsFiniteFloat(width) || !IsFiniteFloat(height))
            {
                return false;
            }

            float min_x;
            float min_y;
            float max_x;
            float max_y;
            InitAccumulator(&min_x, &min_y, &max_x, &max_y);

            if (width <= 0.0f && height <= 0.0f)
            {
                if (!AccumulateCameraPoint(snapshot, dmVMath::Vector3(x, y, z), &min_x, &min_y, &max_x, &max_y))
                {
                    return false;
                }
            }
            else
            {
                float left = x - width * node->m_AnchorX;
                float right = left + width;
                float bottom = y - height * node->m_AnchorY;
                float top = bottom + height;

                const dmVMath::Vector3 points[] = {
                    dmVMath::Vector3(left, bottom, z),
                    dmVMath::Vector3(right, bottom, z),
                    dmVMath::Vector3(right, top, z),
                    dmVMath::Vector3(left, top, z),
                };

                for (uint32_t i = 0; i < DM_ARRAY_SIZE(points); ++i)
                {
                    if (!AccumulateCameraPoint(snapshot, points[i], &min_x, &min_y, &max_x, &max_y))
                    {
                        return false;
                    }
                }
            }

            return SetBoundsFromAccumulator(min_x, min_y, max_x, max_y, snapshot, out_bounds);
        }

        static HWindow GetAutomationBridgeWindow()
        {
            if (!g_AutomationBridge.m_GraphicsContext)
            {
                return 0;
            }
            return dmGraphics::GetWindow(g_AutomationBridge.m_GraphicsContext);
        }

        static void RestoreInputVisualizationGraphicsState(dmGraphics::HContext graphics_context, const dmGraphics::PipelineState& previous_state, int32_t viewport_x, int32_t viewport_y, uint32_t viewport_width, uint32_t viewport_height)
        {
            if (previous_state.m_BlendEnabled)
            {
                dmGraphics::EnableState(graphics_context, dmGraphics::STATE_BLEND);
            }
            else
            {
                dmGraphics::DisableState(graphics_context, dmGraphics::STATE_BLEND);
            }

            if (previous_state.m_DepthTestEnabled)
            {
                dmGraphics::EnableState(graphics_context, dmGraphics::STATE_DEPTH_TEST);
            }
            else
            {
                dmGraphics::DisableState(graphics_context, dmGraphics::STATE_DEPTH_TEST);
            }

            dmGraphics::SetBlendFuncSeparate(
                graphics_context,
                (dmGraphics::BlendFactor)previous_state.m_BlendSrcFactor,
                (dmGraphics::BlendFactor)previous_state.m_BlendDstFactor,
                (dmGraphics::BlendFactor)previous_state.m_BlendSrcFactorAlpha,
                (dmGraphics::BlendFactor)previous_state.m_BlendDstFactorAlpha);
            dmGraphics::SetViewport(graphics_context, viewport_x, viewport_y, viewport_width, viewport_height);
        }

        static void PrepareInputVisualizationRenderState(dmGraphics::HContext graphics_context, dmRender::HRenderContext render_context, uint32_t window_width, uint32_t window_height)
        {
            dmGraphics::SetViewport(graphics_context, 0, 0, window_width, window_height);
            dmGraphics::EnableState(graphics_context, dmGraphics::STATE_BLEND);
            dmGraphics::DisableState(graphics_context, dmGraphics::STATE_DEPTH_TEST);
            dmGraphics::SetBlendFuncSeparate(
                graphics_context,
                dmGraphics::BLEND_FACTOR_SRC_ALPHA,
                dmGraphics::BLEND_FACTOR_ONE_MINUS_SRC_ALPHA,
                dmGraphics::BLEND_FACTOR_ONE,
                dmGraphics::BLEND_FACTOR_ONE_MINUS_SRC_ALPHA);

            dmRender::SetViewMatrix(render_context, dmVMath::Matrix4::identity());
            dmRender::SetProjectionMatrix(render_context, dmVMath::Matrix4::orthographic(0.0f, (float)window_width, 0.0f, (float)window_height, 1.0f, -1.0f));
        }
    }

    void DefoldPrivateApiInitialize(dmExtension::Params* params)
    {
        g_AutomationBridge.m_RenderContext = params ? ExtensionParamsGetContextByName((ExtensionParams*)params, "render") : 0;
    }

    bool DefoldPrivateApiCanSetWindowSize()
    {
        return IsDesktopWindowResizeSupported() && GetAutomationBridgeWindow() != 0;
    }

    bool DefoldPrivateApiSetWindowSize(uint32_t width, uint32_t height)
    {
        if (!DefoldPrivateApiCanSetWindowSize() || width == 0 || height == 0)
        {
            return false;
        }

        HWindow window = GetAutomationBridgeWindow();
        if (!window)
        {
            return false;
        }

        // Defold reports screen/window metadata in framebuffer pixels, while
        // GLFW's platform setter takes window coordinates on high-DPI displays.
        float scale = dmPlatform::GetDisplayScaleFactor(window);
        if (!IsFiniteFloat(scale) || scale < 1.0f)
        {
            scale = 1.0f;
        }

        uint32_t window_width = (uint32_t)MaxFloat(1.0f, floorf((float)width / scale + 0.5f));
        uint32_t window_height = (uint32_t)MaxFloat(1.0f, floorf((float)height / scale + 0.5f));
        dmPlatform::SetWindowSize(window, window_width, window_height);
        return CurrentWindowSizeMatches(width, height, scale);
    }

    void DefoldPrivateApiResetSnapshotCamera()
    {
        g_SnapshotCamera = 0;
    }

    void DefoldPrivateApiUseSnapshotCamera(const dmGameObject::SceneNode* scene_node)
    {
        if (!scene_node || scene_node->m_Component == 0)
        {
            return;
        }

        CameraComponentPrivateApi* camera = (CameraComponentPrivateApi*)scene_node->m_Component;
        g_SnapshotCamera = camera->m_RenderCamera;
    }

    bool DefoldPrivateApiComputeBounds(const dmGameObject::SceneNode* scene_node, const Node* node, const Snapshot* snapshot, Bounds* out_bounds)
    {
        if (!node || !out_bounds || !IsUsableScreen(snapshot))
        {
            return false;
        }

        memset(out_bounds, 0, sizeof(*out_bounds));

        if (StringsEqual(node->m_Kind, "gui_node") && ComputeGuiBounds(scene_node, node, snapshot, out_bounds))
        {
            return true;
        }

        if (!StringsEqual(node->m_Kind, "gui_node") && ComputeCameraBounds(node, snapshot, out_bounds))
        {
            return true;
        }

        return false;
    }

    void DefoldPrivateApiDrawInputVisualization(InputVisualization* visualization)
    {
        if (!visualization || !visualization->m_Active || !g_AutomationBridge.m_RenderContext || !g_AutomationBridge.m_GraphicsContext)
        {
            return;
        }

        uint32_t window_width = dmGraphics::GetWindowWidth(g_AutomationBridge.m_GraphicsContext);
        uint32_t window_height = dmGraphics::GetWindowHeight(g_AutomationBridge.m_GraphicsContext);
        if (window_width == 0 || window_height == 0)
        {
            return;
        }

        int32_t previous_viewport_x = 0;
        int32_t previous_viewport_y = 0;
        uint32_t previous_viewport_width = 0;
        uint32_t previous_viewport_height = 0;
        dmGraphics::GetViewport(g_AutomationBridge.m_GraphicsContext, &previous_viewport_x, &previous_viewport_y, &previous_viewport_width, &previous_viewport_height);
        dmGraphics::PipelineState previous_state = dmGraphics::GetPipelineState(g_AutomationBridge.m_GraphicsContext);

        float duration = visualization->m_Duration > 0.0f ? visualization->m_Duration : 1.0f;
        float t = ClampFloat(visualization->m_Age / duration, 0.0f, 1.0f);
        float alpha = 1.0f - t;
        dmRender::HRenderContext render_context = (dmRender::HRenderContext)g_AutomationBridge.m_RenderContext;

        dmRender::RenderListBegin(render_context);
        PrepareInputVisualizationRenderState(g_AutomationBridge.m_GraphicsContext, render_context, window_width, window_height);

        if (visualization->m_Drag)
        {
            dmVMath::Vector4 color(1.0f, 0.72f, 0.12f, alpha);
            DrawDebugLine(render_context, visualization->m_X1, visualization->m_Y1, visualization->m_X2, visualization->m_Y2, color);
            DrawDebugCircle(render_context, visualization->m_X1, visualization->m_Y1, 6.0f, color);
            DrawDebugCircle(render_context, visualization->m_X2, visualization->m_Y2, 6.0f, color);
            dmRender::RenderListEnd(render_context);
            dmRender::DrawDebug3d(render_context, 0);
            dmRender::ClearRenderObjects(render_context);
            RestoreInputVisualizationGraphicsState(g_AutomationBridge.m_GraphicsContext, previous_state, previous_viewport_x, previous_viewport_y, previous_viewport_width, previous_viewport_height);
            AdvanceInputVisualization(visualization);
            return;
        }

        float radius = 6.0f + 34.0f * t;
        dmVMath::Vector4 color(0.15f, 0.85f, 1.0f, alpha);
        DrawDebugCircle(render_context, visualization->m_X1, visualization->m_Y1, radius, color);
        dmRender::RenderListEnd(render_context);
        dmRender::DrawDebug3d(render_context, 0);
        dmRender::ClearRenderObjects(render_context);
        RestoreInputVisualizationGraphicsState(g_AutomationBridge.m_GraphicsContext, previous_state, previous_viewport_x, previous_viewport_y, previous_viewport_width, previous_viewport_height);

        AdvanceInputVisualization(visualization);
    }

}

#endif
