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
    const dmVMath::Matrix4& GetViewProjectionMatrix(HRenderContext render_context);
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

        static bool ProjectWithViewProjection(const dmVMath::Matrix4& view_proj, const Snapshot* snapshot, const dmVMath::Vector3& world, float* out_x, float* out_y)
        {
            dmVMath::Vector4 clip = view_proj * dmVMath::Vector4(world.getX(), world.getY(), world.getZ(), 1.0f);
            float w = clip.getW();
            if (w > -0.000001f && w < 0.000001f)
            {
                return false;
            }

            float inv_w = 1.0f / w;
            float ndc_x = clip.getX() * inv_w;
            float ndc_y = clip.getY() * inv_w;

            float viewport_x = (float)snapshot->m_ViewportX;
            float viewport_y = (float)snapshot->m_ViewportY;
            float viewport_w = (float)snapshot->m_ViewportWidth;
            float viewport_h = (float)snapshot->m_ViewportHeight;
            if (viewport_w <= 0.0f || viewport_h <= 0.0f)
            {
                viewport_x = 0.0f;
                viewport_y = 0.0f;
                viewport_w = (float)snapshot->m_WindowWidth;
                viewport_h = (float)snapshot->m_WindowHeight;
            }

            float bottom_left_x = viewport_x + (ndc_x + 1.0f) * 0.5f * viewport_w;
            float bottom_left_y = viewport_y + (ndc_y + 1.0f) * 0.5f * viewport_h;
            *out_x = bottom_left_x;
            *out_y = (float)snapshot->m_WindowHeight - bottom_left_y;
            return IsUsablePoint(*out_x, *out_y);
        }

        static bool ComputeRenderBounds(const Node* node, const Snapshot* snapshot, Bounds* out_bounds)
        {
            if (!g_AutomationBridge.m_RenderContext || !node->m_HasPosition)
            {
                return false;
            }

            dmRender::HRenderContext render_context = (dmRender::HRenderContext)g_AutomationBridge.m_RenderContext;
            const dmVMath::Matrix4& view_proj = dmRender::GetViewProjectionMatrix(render_context);

            float half_w = node->m_HasSize ? fabsf(node->m_Size[0]) * 0.5f : 0.0f;
            float half_h = node->m_HasSize ? fabsf(node->m_Size[1]) * 0.5f : 0.0f;
            float x = node->m_Position[0];
            float y = node->m_Position[1];
            float z = node->m_Position[2];

            float min_x;
            float min_y;
            float max_x;
            float max_y;
            InitAccumulator(&min_x, &min_y, &max_x, &max_y);

            const dmVMath::Vector3 points[] = {
                dmVMath::Vector3(x - half_w, y - half_h, z),
                dmVMath::Vector3(x + half_w, y - half_h, z),
                dmVMath::Vector3(x + half_w, y + half_h, z),
                dmVMath::Vector3(x - half_w, y + half_h, z),
            };

            for (uint32_t i = 0; i < DM_ARRAY_SIZE(points); ++i)
            {
                float screen_x;
                float screen_y;
                if (!ProjectWithViewProjection(view_proj, snapshot, points[i], &screen_x, &screen_y))
                {
                    return false;
                }
                AccumulatePoint(screen_x, screen_y, &min_x, &min_y, &max_x, &max_y);
            }

            return SetBoundsFromAccumulator(min_x, min_y, max_x, max_y, snapshot, out_bounds);
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
    }

    void DefoldPrivateApiInitialize(dmExtension::Params* params)
    {
        g_AutomationBridge.m_RenderContext = params ? ExtensionParamsGetContextByName((ExtensionParams*)params, "render") : 0;
    }

    bool DefoldPrivateApiSetWindowSize(uint32_t width, uint32_t height)
    {
        if (!g_AutomationBridge.m_GraphicsContext || width == 0 || height == 0)
        {
            return false;
        }

        HWindow window = dmGraphics::GetWindow(g_AutomationBridge.m_GraphicsContext);
        if (!window)
        {
            return false;
        }

        // Defold reports screen/window metadata in framebuffer pixels, while
        // GLFW's platform setter takes window coordinates on high-DPI displays.
        float scale = dmPlatform::GetDisplayScaleFactor(window);
        if (!IsFiniteFloat(scale) || scale <= 0.0f)
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

        if (!StringsEqual(node->m_Kind, "gui_node") && ComputeRenderBounds(node, snapshot, out_bounds))
        {
            return true;
        }

        return false;
    }
}

#endif
