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

        static bool ScreenToDebugWorld(float x, float y, dmVMath::Point3* out)
        {
            if (!g_AutomationBridge.m_GraphicsContext)
            {
                return false;
            }

            uint32_t window_width = dmGraphics::GetWindowWidth(g_AutomationBridge.m_GraphicsContext);
            uint32_t window_height = dmGraphics::GetWindowHeight(g_AutomationBridge.m_GraphicsContext);
            uint32_t render_width = dmGraphics::GetWidth(g_AutomationBridge.m_GraphicsContext);
            uint32_t render_height = dmGraphics::GetHeight(g_AutomationBridge.m_GraphicsContext);
            if (window_width == 0 || window_height == 0 || render_width == 0 || render_height == 0)
            {
                return false;
            }

            float world_x = x * ((float)render_width / (float)window_width);
            float world_y = ((float)window_height - y) * ((float)render_height / (float)window_height);
            *out = dmVMath::Point3(world_x, world_y, 0.0f);
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

        static HWindow GetAutomationBridgeWindow()
        {
            if (!g_AutomationBridge.m_GraphicsContext)
            {
                return 0;
            }
            return dmGraphics::GetWindow(g_AutomationBridge.m_GraphicsContext);
        }
    }

    void DefoldPrivateApiInitialize(dmExtension::Params* params)
    {
        g_AutomationBridge.m_RenderContext = params ? ExtensionParamsGetContextByName((ExtensionParams*)params, "render") : 0;
    }

    bool DefoldPrivateApiCanSetWindowSize()
    {
        return GetAutomationBridgeWindow() != 0;
    }

    bool DefoldPrivateApiSetWindowSize(uint32_t width, uint32_t height)
    {
        if (!g_AutomationBridge.m_GraphicsContext || width == 0 || height == 0)
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
        return true;
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

        return false;
    }

    void DefoldPrivateApiDrawInputVisualization(InputVisualization* visualization)
    {
        if (!visualization || !visualization->m_Active || !g_AutomationBridge.m_RenderContext)
        {
            return;
        }

        float duration = visualization->m_Duration > 0.0f ? visualization->m_Duration : 1.0f;
        float t = ClampFloat(visualization->m_Age / duration, 0.0f, 1.0f);
        float alpha = 1.0f - t;
        dmRender::HRenderContext render_context = (dmRender::HRenderContext)g_AutomationBridge.m_RenderContext;

        if (visualization->m_Drag)
        {
            dmVMath::Vector4 color(1.0f, 0.72f, 0.12f, alpha);
            DrawDebugLine(render_context, visualization->m_X1, visualization->m_Y1, visualization->m_X2, visualization->m_Y2, color);
            DrawDebugCircle(render_context, visualization->m_X1, visualization->m_Y1, 6.0f, color);
            DrawDebugCircle(render_context, visualization->m_X2, visualization->m_Y2, 6.0f, color);
            AdvanceInputVisualization(visualization);
            return;
        }

        float radius = 6.0f + 34.0f * t;
        dmVMath::Vector4 color(0.15f, 0.85f, 1.0f, alpha);
        DrawDebugCircle(render_context, visualization->m_X1, visualization->m_Y1, radius, color);

        AdvanceInputVisualization(visualization);
    }

}

#endif
