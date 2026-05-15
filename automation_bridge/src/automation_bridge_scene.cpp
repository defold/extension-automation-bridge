#include "automation_bridge_private.h"
#include "automation_bridge_defold_private_api.h"

#if defined(DM_DEBUG)

#include <math.h>
#include <stdio.h>
#include <string.h>

namespace dmAutomationBridge
{
    static const char* SceneNodeTypeToString(dmGameObject::SceneNodeType type)
    {
        switch (type)
        {
        case dmGameObject::SCENE_NODE_TYPE_COLLECTION:
            return "collectionc";
        case dmGameObject::SCENE_NODE_TYPE_GAMEOBJECT:
            return "goc";
        case dmGameObject::SCENE_NODE_TYPE_COMPONENT:
            return "component";
        case dmGameObject::SCENE_NODE_TYPE_SUBCOMPONENT:
            return "subcomponent";
        default:
            return "node";
        }
    }

    static const char* KindFromType(dmGameObject::SceneNodeType scene_node_type, const char* type)
    {
        if (StartsWith(type, "gui_node_"))
        {
            return "gui_node";
        }
        if (StringsEqual(type, "goc") || scene_node_type == dmGameObject::SCENE_NODE_TYPE_GAMEOBJECT)
        {
            return "game_object";
        }
        if (StringsEqual(type, "collectionc") || scene_node_type == dmGameObject::SCENE_NODE_TYPE_COLLECTION)
        {
            return "collection";
        }
        if (scene_node_type == dmGameObject::SCENE_NODE_TYPE_COMPONENT)
        {
            return "component";
        }
        if (scene_node_type == dmGameObject::SCENE_NODE_TYPE_SUBCOMPONENT)
        {
            return "subcomponent";
        }
        return "node";
    }

    static void SetAnchorFromPivot(Node* node, const char* pivot)
    {
        if (StringsEqual(pivot, "PIVOT_NW"))
        {
            node->m_AnchorX = 0.0f;
            node->m_AnchorY = 0.0f;
        }
        else if (StringsEqual(pivot, "PIVOT_N"))
        {
            node->m_AnchorX = 0.5f;
            node->m_AnchorY = 0.0f;
        }
        else if (StringsEqual(pivot, "PIVOT_NE"))
        {
            node->m_AnchorX = 1.0f;
            node->m_AnchorY = 0.0f;
        }
        else if (StringsEqual(pivot, "PIVOT_W"))
        {
            node->m_AnchorX = 0.0f;
            node->m_AnchorY = 0.5f;
        }
        else if (StringsEqual(pivot, "PIVOT_CENTER"))
        {
            node->m_AnchorX = 0.5f;
            node->m_AnchorY = 0.5f;
        }
        else if (StringsEqual(pivot, "PIVOT_E"))
        {
            node->m_AnchorX = 1.0f;
            node->m_AnchorY = 0.5f;
        }
        else if (StringsEqual(pivot, "PIVOT_SW"))
        {
            node->m_AnchorX = 0.0f;
            node->m_AnchorY = 1.0f;
        }
        else if (StringsEqual(pivot, "PIVOT_S"))
        {
            node->m_AnchorX = 0.5f;
            node->m_AnchorY = 1.0f;
        }
        else if (StringsEqual(pivot, "PIVOT_SE"))
        {
            node->m_AnchorX = 1.0f;
            node->m_AnchorY = 1.0f;
        }
    }

    static void FreeProperty(Property* property)
    {
        FreeString(&property->m_Name);
        FreeString(&property->m_Json);
        FreeString(&property->m_StringValue);
    }

    static bool SetPropertyJsonString(Property* property, const char* value)
    {
        StringBuffer json;
        StringBufferInit(&json);
        AppendJsonString(&json, value);
        free(property->m_Json);
        property->m_Json = StringBufferDetach(&json);
        return property->m_Json != 0;
    }

    static bool MakeProperty(Property* property, const dmGameObject::SceneNodeProperty* source)
    {
        memset(property, 0, sizeof(*property));
        if (!SetHashString(&property->m_Name, source->m_NameHash))
        {
            return false;
        }

        switch (source->m_Type)
        {
        case dmGameObject::SCENE_NODE_PROPERTY_TYPE_HASH:
            if (!SetHashString(&property->m_StringValue, source->m_Value.m_Hash))
            {
                return false;
            }
            SetPropertyJsonString(property, property->m_StringValue);
            break;
        case dmGameObject::SCENE_NODE_PROPERTY_TYPE_NUMBER:
        {
            StringBuffer json;
            StringBufferInit(&json);
            AppendNumber(&json, source->m_Value.m_Number);
            property->m_Json = StringBufferDetach(&json);
            break;
        }
        case dmGameObject::SCENE_NODE_PROPERTY_TYPE_BOOLEAN:
            property->m_BoolValue = source->m_Value.m_Bool;
            property->m_HasBool = true;
            SetString(&property->m_Json, source->m_Value.m_Bool ? "true" : "false");
            SetString(&property->m_StringValue, source->m_Value.m_Bool ? "true" : "false");
            break;
        case dmGameObject::SCENE_NODE_PROPERTY_TYPE_URL:
            SetString(&property->m_StringValue, source->m_Value.m_URL);
            SetPropertyJsonString(property, property->m_StringValue);
            break;
        case dmGameObject::SCENE_NODE_PROPERTY_TYPE_TEXT:
            SetString(&property->m_StringValue, source->m_Value.m_Text ? source->m_Value.m_Text : "");
            SetPropertyJsonString(property, property->m_StringValue);
            break;
        case dmGameObject::SCENE_NODE_PROPERTY_TYPE_VECTOR3:
        case dmGameObject::SCENE_NODE_PROPERTY_TYPE_VECTOR4:
        case dmGameObject::SCENE_NODE_PROPERTY_TYPE_QUAT:
        {
            property->m_HasVector = true;
            property->m_VectorCount = source->m_Type == dmGameObject::SCENE_NODE_PROPERTY_TYPE_VECTOR3 ? 3 : 4;

            StringBuffer json;
            StringBufferInit(&json);
            StringBufferAppendChar(&json, '[');
            for (uint32_t i = 0; i < property->m_VectorCount; ++i)
            {
                property->m_Vector[i] = source->m_Value.m_V4[i];
                if (i > 0)
                {
                    StringBufferAppendChar(&json, ',');
                }
                AppendNumber(&json, property->m_Vector[i]);
            }
            StringBufferAppendChar(&json, ']');
            property->m_Json = StringBufferDetach(&json);
            break;
        }
        default:
            SetString(&property->m_Json, "null");
            break;
        }

        return property->m_Json != 0;
    }

    static void InitNode(Node* node)
    {
        memset(node, 0, sizeof(*node));
        node->m_Visible = true;
        node->m_Enabled = true;
        node->m_AnchorX = 0.5f;
        node->m_AnchorY = 0.5f;
    }

    static void FreeNode(Node* node)
    {
        FreeString(&node->m_Id);
        FreeString(&node->m_Name);
        FreeString(&node->m_Type);
        FreeString(&node->m_Kind);
        FreeString(&node->m_Path);
        FreeString(&node->m_Parent);
        FreeString(&node->m_Text);
        FreeString(&node->m_Url);
        FreeString(&node->m_Resource);
        for (uint32_t i = 0; i < node->m_Properties.m_Count; ++i)
        {
            FreeProperty(&node->m_Properties.m_Data[i]);
        }
        ArrayFree(&node->m_Properties);
        ArrayFree(&node->m_Children);
    }

    static void ApplyPropertyToNode(Node* node, const Property* property)
    {
        if (StringsEqual(property->m_Name, "id") && !IsEmpty(property->m_StringValue))
        {
            SetString(&node->m_Name, property->m_StringValue);
        }
        else if (StringsEqual(property->m_Name, "type") && !IsEmpty(property->m_StringValue))
        {
            SetString(&node->m_Type, property->m_StringValue);
        }
        else if (StringsEqual(property->m_Name, "text") && !IsEmpty(property->m_StringValue))
        {
            SetString(&node->m_Text, property->m_StringValue);
        }
        else if (StringsEqual(property->m_Name, "url") && !IsEmpty(property->m_StringValue))
        {
            SetString(&node->m_Url, property->m_StringValue);
        }
        else if (StringsEqual(property->m_Name, "resource") && !IsEmpty(property->m_StringValue))
        {
            SetString(&node->m_Resource, property->m_StringValue);
        }
        else if (StringsEqual(property->m_Name, "visible") && property->m_HasBool)
        {
            node->m_Visible = property->m_BoolValue;
        }
        else if (StringsEqual(property->m_Name, "enabled") && property->m_HasBool)
        {
            node->m_Enabled = property->m_BoolValue;
        }
        else if (StringsEqual(property->m_Name, "pivot") && !IsEmpty(property->m_StringValue))
        {
            SetAnchorFromPivot(node, property->m_StringValue);
        }

        if (property->m_HasVector && property->m_VectorCount >= 2)
        {
            if (StringsEqual(property->m_Name, "world_position") || (!node->m_HasPosition && StringsEqual(property->m_Name, "position")))
            {
                node->m_HasPosition = true;
                node->m_Position[0] = property->m_Vector[0];
                node->m_Position[1] = property->m_Vector[1];
                node->m_Position[2] = property->m_VectorCount >= 3 ? property->m_Vector[2] : 0.0f;
            }
            else if (StringsEqual(property->m_Name, "world_size") || (!node->m_HasSize && StringsEqual(property->m_Name, "size")))
            {
                node->m_HasSize = true;
                node->m_Size[0] = property->m_Vector[0];
                node->m_Size[1] = property->m_Vector[1];
                node->m_Size[2] = property->m_VectorCount >= 3 ? property->m_Vector[2] : 0.0f;
            }
        }
    }

    static void ComputeFallbackBounds(Node* node, uint32_t screen_w, uint32_t screen_h, uint32_t display_w, uint32_t display_h)
    {
        if (!node->m_HasPosition || screen_w == 0 || screen_h == 0)
        {
            return;
        }

        float width = node->m_HasSize ? fabsf(node->m_Size[0]) : 0.0f;
        float height = node->m_HasSize ? fabsf(node->m_Size[1]) : 0.0f;
        float x = node->m_Position[0];
        float y = node->m_Position[1];

        if (!IsFiniteFloat(x) || !IsFiniteFloat(y) || !IsFiniteFloat(width) || !IsFiniteFloat(height))
        {
            return;
        }

        if (StringsEqual(node->m_Kind, "gui_node"))
        {
            node->m_Bounds.m_X = x - width * node->m_AnchorX;
            node->m_Bounds.m_Y = (float)screen_h - (y + height * node->m_AnchorY);
            node->m_Bounds.m_W = width;
            node->m_Bounds.m_H = height;
            node->m_Bounds.m_CX = node->m_Bounds.m_X + width * 0.5f;
            node->m_Bounds.m_CY = node->m_Bounds.m_Y + height * 0.5f;
        }
        else
        {
            float source_w = display_w > 0 ? (float)display_w : (float)screen_w;
            float source_h = display_h > 0 ? (float)display_h : (float)screen_h;
            float scale_x = source_w > 0.0f ? (float)screen_w / source_w : 1.0f;
            float scale_y = source_h > 0.0f ? (float)screen_h / source_h : 1.0f;
            node->m_Bounds.m_CX = x * scale_x;
            node->m_Bounds.m_CY = (source_h - y) * scale_y;
            node->m_Bounds.m_W = width * scale_x;
            node->m_Bounds.m_H = height * scale_y;
            node->m_Bounds.m_X = node->m_Bounds.m_CX - node->m_Bounds.m_W * 0.5f;
            node->m_Bounds.m_Y = node->m_Bounds.m_CY - node->m_Bounds.m_H * 0.5f;
        }

        node->m_Bounds.m_NX = node->m_Bounds.m_CX / (float)screen_w;
        node->m_Bounds.m_NY = node->m_Bounds.m_CY / (float)screen_h;
        node->m_Bounds.m_Valid = true;
    }

    void InitSnapshot(Snapshot* snapshot)
    {
        memset(snapshot, 0, sizeof(*snapshot));
        snapshot->m_Root = -1;
    }

    void FreeSnapshot(Snapshot* snapshot)
    {
        for (uint32_t i = 0; i < snapshot->m_Nodes.m_Count; ++i)
        {
            FreeNode(&snapshot->m_Nodes.m_Data[i]);
        }
        ArrayFree(&snapshot->m_Nodes);
        InitSnapshot(snapshot);
    }

    static int32_t BuildNode(Snapshot* snapshot, dmGameObject::SceneNode* scene_node, int32_t parent_index, const char* parent_path, uint32_t sibling_index)
    {
        Node node;
        InitNode(&node);

        dmGameObject::SceneNodePropertyIterator pit = dmGameObject::TraverseIterateProperties(scene_node);
        while (dmGameObject::TraverseIteratePropertiesNext(&pit))
        {
            Property property;
            if (MakeProperty(&property, &pit.m_Property))
            {
                ApplyPropertyToNode(&node, &property);
                if (!ArrayPush(&node.m_Properties, &property))
                {
                    FreeProperty(&property);
                }
            }
        }

        if (IsEmpty(node.m_Type))
        {
            SetString(&node.m_Type, SceneNodeTypeToString(scene_node->m_Type));
        }
        SetString(&node.m_Kind, KindFromType(scene_node->m_Type, node.m_Type));

        if (IsEmpty(node.m_Name))
        {
            char buffer[64];
            dmSnPrintf(buffer, sizeof(buffer), "%s_%u", node.m_Type, sibling_index);
            SetString(&node.m_Name, buffer);
        }

        const char* safe_segment = IsEmpty(node.m_Name) ? node.m_Type : node.m_Name;
        char segment[128];
        dmSnPrintf(segment, sizeof(segment), "%s[%u]", safe_segment, sibling_index);

        StringBuffer path;
        StringBufferInit(&path);
        if (IsEmpty(parent_path))
        {
            StringBufferAppendChar(&path, '/');
            StringBufferAppend(&path, segment);
        }
        else
        {
            StringBufferAppend(&path, parent_path);
            StringBufferAppendChar(&path, '/');
            StringBufferAppend(&path, segment);
        }
        node.m_Path = StringBufferDetach(&path);

        uint64_t id_hash = dmHashBuffer64(node.m_Path, (uint32_t)strlen(node.m_Path));
        char id[32];
        dmSnPrintf(id, sizeof(id), "n:%016llx", (unsigned long long)id_hash);
        SetString(&node.m_Id, id);

        if (parent_index >= 0 && (uint32_t)parent_index < snapshot->m_Nodes.m_Count)
        {
            SetString(&node.m_Parent, snapshot->m_Nodes.m_Data[parent_index].m_Id);
        }

        Bounds private_bounds;
        if (DefoldPrivateApiComputeBounds(scene_node, &node, snapshot, &private_bounds))
        {
            node.m_Bounds = private_bounds;
        }
        else
        {
            ComputeFallbackBounds(&node, snapshot->m_WindowWidth, snapshot->m_WindowHeight, snapshot->m_DisplayWidth, snapshot->m_DisplayHeight);
        }

        uint32_t index = snapshot->m_Nodes.m_Count;
        if (!ArrayPush(&snapshot->m_Nodes, &node))
        {
            FreeNode(&node);
            return -1;
        }

        dmGameObject::SceneNodeIterator it = dmGameObject::TraverseIterateChildren(scene_node);
        uint32_t child_index = 0;
        while (dmGameObject::TraverseIterateNext(&it))
        {
            int32_t child = BuildNode(snapshot, &it.m_Node, (int32_t)index, snapshot->m_Nodes.m_Data[index].m_Path, child_index++);
            if (child >= 0)
            {
                uint32_t child_u32 = (uint32_t)child;
                ArrayPush(&snapshot->m_Nodes.m_Data[index].m_Children, &child_u32);
            }
        }

        return (int32_t)index;
    }

    static void RefreshScreenInfo(Snapshot* snapshot)
    {
        snapshot->m_DisplayWidth = g_AutomationBridge.m_DisplayWidth;
        snapshot->m_DisplayHeight = g_AutomationBridge.m_DisplayHeight;

        if (!g_AutomationBridge.m_GraphicsContext)
        {
            return;
        }

        snapshot->m_WindowWidth = dmGraphics::GetWindowWidth(g_AutomationBridge.m_GraphicsContext);
        snapshot->m_WindowHeight = dmGraphics::GetWindowHeight(g_AutomationBridge.m_GraphicsContext);
        snapshot->m_BackbufferWidth = dmGraphics::GetWidth(g_AutomationBridge.m_GraphicsContext);
        snapshot->m_BackbufferHeight = dmGraphics::GetHeight(g_AutomationBridge.m_GraphicsContext);
        dmGraphics::GetViewport(g_AutomationBridge.m_GraphicsContext, &snapshot->m_ViewportX, &snapshot->m_ViewportY, &snapshot->m_ViewportWidth, &snapshot->m_ViewportHeight);

        if (snapshot->m_WindowWidth == 0)
        {
            snapshot->m_WindowWidth = snapshot->m_ViewportWidth;
        }
        if (snapshot->m_WindowHeight == 0)
        {
            snapshot->m_WindowHeight = snapshot->m_ViewportHeight;
        }
    }

    void UpdateSnapshot()
    {
        Snapshot snapshot;
        InitSnapshot(&snapshot);
        snapshot.m_Sequence = g_AutomationBridge.m_Snapshot.m_Sequence + 1;
        RefreshScreenInfo(&snapshot);

        if (g_AutomationBridge.m_Register)
        {
            dmGameObject::SceneNode root;
            if (dmGameObject::TraverseGetRoot(g_AutomationBridge.m_Register, &root))
            {
                snapshot.m_Root = BuildNode(&snapshot, &root, -1, "", 0);
            }
        }

        FreeSnapshot(&g_AutomationBridge.m_Snapshot);
        g_AutomationBridge.m_Snapshot = snapshot;
    }

    const Node* FindNodeById(const char* id)
    {
        const Snapshot* snapshot = &g_AutomationBridge.m_Snapshot;
        for (uint32_t i = 0; i < snapshot->m_Nodes.m_Count; ++i)
        {
            if (StringsEqual(snapshot->m_Nodes.m_Data[i].m_Id, id))
            {
                return &snapshot->m_Nodes.m_Data[i];
            }
        }
        return 0;
    }

    static void AppendBoundsJson(StringBuffer* out, const Bounds* bounds)
    {
        if (!bounds->m_Valid)
        {
            StringBufferAppend(out, "null");
            return;
        }

        StringBufferAppend(out, "{\"screen\":{\"x\":");
        AppendNumber(out, bounds->m_X);
        StringBufferAppend(out, ",\"y\":");
        AppendNumber(out, bounds->m_Y);
        StringBufferAppend(out, ",\"w\":");
        AppendNumber(out, bounds->m_W);
        StringBufferAppend(out, ",\"h\":");
        AppendNumber(out, bounds->m_H);
        StringBufferAppend(out, "},\"center\":{\"x\":");
        AppendNumber(out, bounds->m_CX);
        StringBufferAppend(out, ",\"y\":");
        AppendNumber(out, bounds->m_CY);
        StringBufferAppend(out, "},\"normalized\":{\"x\":");
        AppendNumber(out, bounds->m_NX);
        StringBufferAppend(out, ",\"y\":");
        AppendNumber(out, bounds->m_NY);
        StringBufferAppend(out, "}}");
    }

    void AppendNodeJson(StringBuffer* out, const Snapshot* snapshot, const Node* node, const IncludeOptions* include, bool recursive, bool visible_only)
    {
        StringBufferAppend(out, "{\"id\":");
        AppendJsonString(out, node->m_Id);
        StringBufferAppend(out, ",\"name\":");
        AppendJsonString(out, node->m_Name);
        StringBufferAppend(out, ",\"type\":");
        AppendJsonString(out, node->m_Type);
        StringBufferAppend(out, ",\"kind\":");
        AppendJsonString(out, node->m_Kind);
        StringBufferAppend(out, ",\"path\":");
        AppendJsonString(out, node->m_Path);
        StringBufferAppend(out, ",\"parent\":");
        if (IsEmpty(node->m_Parent))
        {
            StringBufferAppend(out, "null");
        }
        else
        {
            AppendJsonString(out, node->m_Parent);
        }
        StringBufferAppend(out, ",\"visible\":");
        StringBufferAppend(out, node->m_Visible ? "true" : "false");
        StringBufferAppend(out, ",\"enabled\":");
        StringBufferAppend(out, node->m_Enabled ? "true" : "false");

        if (!IsEmpty(node->m_Text))
        {
            StringBufferAppend(out, ",\"text\":");
            AppendJsonString(out, node->m_Text);
        }
        if (!IsEmpty(node->m_Url))
        {
            StringBufferAppend(out, ",\"url\":");
            AppendJsonString(out, node->m_Url);
        }
        if (!IsEmpty(node->m_Resource))
        {
            StringBufferAppend(out, ",\"resource\":");
            AppendJsonString(out, node->m_Resource);
        }

        if (include->m_Bounds)
        {
            StringBufferAppend(out, ",\"bounds\":");
            AppendBoundsJson(out, &node->m_Bounds);
        }

        if (include->m_Properties)
        {
            StringBufferAppend(out, ",\"properties\":{");
            for (uint32_t i = 0; i < node->m_Properties.m_Count; ++i)
            {
                if (i > 0)
                {
                    StringBufferAppendChar(out, ',');
                }
                AppendJsonString(out, node->m_Properties.m_Data[i].m_Name);
                StringBufferAppendChar(out, ':');
                StringBufferAppend(out, node->m_Properties.m_Data[i].m_Json);
            }
            StringBufferAppendChar(out, '}');
        }

        if (include->m_Children || recursive)
        {
            StringBufferAppend(out, ",\"children\":[");
            uint32_t emitted_children = 0;
            for (uint32_t i = 0; i < node->m_Children.m_Count; ++i)
            {
                uint32_t child = node->m_Children.m_Data[i];
                if (child < snapshot->m_Nodes.m_Count)
                {
                    const Node* child_node = &snapshot->m_Nodes.m_Data[child];
                    if (visible_only && !child_node->m_Visible)
                    {
                        continue;
                    }
                    if (emitted_children > 0)
                    {
                        StringBufferAppendChar(out, ',');
                    }
                    AppendNodeJson(out, snapshot, child_node, include, recursive, visible_only);
                    ++emitted_children;
                }
            }
            StringBufferAppendChar(out, ']');
        }

        StringBufferAppendChar(out, '}');
    }

    void AppendScreenJson(StringBuffer* out, const Snapshot* snapshot)
    {
        StringBufferAppend(out, "{\"window\":{\"width\":");
        AppendNumber(out, snapshot->m_WindowWidth);
        StringBufferAppend(out, ",\"height\":");
        AppendNumber(out, snapshot->m_WindowHeight);
        StringBufferAppend(out, "},\"backbuffer\":{\"width\":");
        AppendNumber(out, snapshot->m_BackbufferWidth);
        StringBufferAppend(out, ",\"height\":");
        AppendNumber(out, snapshot->m_BackbufferHeight);
        StringBufferAppend(out, "},\"display\":{\"width\":");
        AppendNumber(out, snapshot->m_DisplayWidth);
        StringBufferAppend(out, ",\"height\":");
        AppendNumber(out, snapshot->m_DisplayHeight);
        StringBufferAppend(out, "},\"viewport\":{\"x\":");
        AppendNumber(out, snapshot->m_ViewportX);
        StringBufferAppend(out, ",\"y\":");
        AppendNumber(out, snapshot->m_ViewportY);
        StringBufferAppend(out, ",\"width\":");
        AppendNumber(out, snapshot->m_ViewportWidth);
        StringBufferAppend(out, ",\"height\":");
        AppendNumber(out, snapshot->m_ViewportHeight);
        StringBufferAppend(out, "},\"coordinates\":{\"origin\":\"top-left\",\"units\":\"screen_pixels\"}}");
    }


}

#endif
