// Debug-only runtime inspection and input API for agent-driven Defold testing.
#if !defined(DM_RELEASE) && !defined(DM_HEADLESS) && !defined(DM_DEBUG)
#define DM_DEBUG
#endif

#include <dmsdk/sdk.h>

#define MODULE_NAME Agent
#define LIB_NAME "agent"

#if defined(DM_DEBUG)

#include <dmsdk/dlib/utf8.h>
#include <dmsdk/dlib/uri.h>

#include <algorithm>
#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <string>
#include <sys/stat.h>
#include <vector>

#if defined(_WIN32)
#include <direct.h>
#endif

namespace dmAgent
{

static const char* API_PREFIX = "/agent/v1";
static const char* API_VERSION = "1";
static const uint32_t MAX_INPUT_EVENTS = 64;
static const uint32_t MAX_KEY_INPUT_BYTES = 4096;

struct QueryParam
{
    std::string m_Key;
    std::string m_Value;
};

struct Property
{
    std::string m_Name;
    std::string m_Json;
    std::string m_StringValue;
    float       m_Vector[4];
    uint8_t     m_VectorCount;
    bool        m_HasVector;
    bool        m_BoolValue;
    bool        m_HasBool;

    Property()
    : m_VectorCount(0)
    , m_HasVector(false)
    , m_BoolValue(false)
    , m_HasBool(false)
    {
        memset(m_Vector, 0, sizeof(m_Vector));
    }
};

struct Bounds
{
    bool  m_Valid;
    float m_X;
    float m_Y;
    float m_W;
    float m_H;
    float m_CX;
    float m_CY;
    float m_NX;
    float m_NY;

    Bounds()
    : m_Valid(false)
    , m_X(0.0f)
    , m_Y(0.0f)
    , m_W(0.0f)
    , m_H(0.0f)
    , m_CX(0.0f)
    , m_CY(0.0f)
    , m_NX(0.0f)
    , m_NY(0.0f)
    {
    }
};

struct Node
{
    std::string         m_Id;
    std::string         m_Name;
    std::string         m_Type;
    std::string         m_Kind;
    std::string         m_Path;
    std::string         m_Parent;
    std::string         m_Text;
    std::string         m_Url;
    std::string         m_Resource;
    bool                m_Visible;
    bool                m_Enabled;
    Bounds              m_Bounds;
    std::vector<Property> m_Properties;
    std::vector<uint32_t> m_Children;

    bool  m_HasPosition;
    bool  m_HasSize;
    float m_Position[3];
    float m_Size[3];
    float m_AnchorX;
    float m_AnchorY;

    Node()
    : m_Visible(true)
    , m_Enabled(true)
    , m_HasPosition(false)
    , m_HasSize(false)
    , m_AnchorX(0.5f)
    , m_AnchorY(0.5f)
    {
        memset(m_Position, 0, sizeof(m_Position));
        memset(m_Size, 0, sizeof(m_Size));
    }
};

struct Snapshot
{
    std::vector<Node> m_Nodes;
    int32_t           m_Root;
    uint32_t          m_WindowWidth;
    uint32_t          m_WindowHeight;
    uint32_t          m_BackbufferWidth;
    uint32_t          m_BackbufferHeight;
    int32_t           m_ViewportX;
    int32_t           m_ViewportY;
    uint32_t          m_ViewportWidth;
    uint32_t          m_ViewportHeight;
    uint64_t          m_Sequence;

    Snapshot()
    : m_Root(-1)
    , m_WindowWidth(0)
    , m_WindowHeight(0)
    , m_BackbufferWidth(0)
    , m_BackbufferHeight(0)
    , m_ViewportX(0)
    , m_ViewportY(0)
    , m_ViewportWidth(0)
    , m_ViewportHeight(0)
    , m_Sequence(0)
    {
    }
};

enum InputEventType
{
    INPUT_EVENT_MOUSE,
    INPUT_EVENT_KEYS
};

struct InputEvent
{
    InputEventType m_Type;
    float          m_X1;
    float          m_Y1;
    float          m_X2;
    float          m_Y2;
    float          m_Duration;
    float          m_Elapsed;
    dmHID::MouseButton m_MouseButton;

    std::string    m_Keys;
    uint32_t       m_KeyIndex;
    dmHID::Key     m_ActiveKey;

    InputEvent()
    : m_Type(INPUT_EVENT_MOUSE)
    , m_X1(0.0f)
    , m_Y1(0.0f)
    , m_X2(0.0f)
    , m_Y2(0.0f)
    , m_Duration(0.0f)
    , m_Elapsed(0.0f)
    , m_MouseButton(dmHID::MOUSE_BUTTON_LEFT)
    , m_KeyIndex(0)
    , m_ActiveKey(dmHID::MAX_KEY_COUNT)
    {
    }
};

struct AgentContext
{
    bool                    m_Initialized;
    dmGameObject::HRegister m_Register;
    dmHID::HContext         m_HidContext;
    dmWebServer::HServer    m_WebServer;
    dmGraphics::HContext    m_GraphicsContext;
    bool                    m_WebHandlerRegistered;
    uint64_t                m_LastTime;
    Snapshot                m_Snapshot;
    std::vector<InputEvent> m_InputEvents;
    bool                    m_ScreenshotPending;
    uint32_t                m_ScreenshotCounter;
    char                    m_ScreenshotPath[1024];

    AgentContext()
    : m_Initialized(false)
    , m_Register(0)
    , m_HidContext(0)
    , m_WebServer(0)
    , m_GraphicsContext(0)
    , m_WebHandlerRegistered(false)
    , m_LastTime(0)
    , m_ScreenshotPending(false)
    , m_ScreenshotCounter(0)
    {
        m_ScreenshotPath[0] = 0;
    }
};

static AgentContext g_Agent;

static bool StartsWith(const std::string& s, const char* prefix)
{
    size_t n = strlen(prefix);
    return s.size() >= n && strncmp(s.c_str(), prefix, n) == 0;
}

static bool IsFinite(float value)
{
    return isfinite(value) != 0;
}

static void AppendNumber(std::string& out, double value)
{
    char buffer[64];
    if (!isfinite(value))
    {
        value = 0.0;
    }
    dmSnPrintf(buffer, sizeof(buffer), "%.9g", value);
    out += buffer;
}

static std::string JsonEscape(const char* value)
{
    std::string out;
    if (!value)
    {
        return out;
    }
    while (*value)
    {
        unsigned char c = (unsigned char)*value++;
        switch (c)
        {
        case '\\': out += "\\\\"; break;
        case '"':  out += "\\\""; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if (c < 32)
            {
                char buffer[8];
                dmSnPrintf(buffer, sizeof(buffer), "\\u%04x", c);
                out += buffer;
            }
            else
            {
                out += (char)c;
            }
            break;
        }
    }
    return out;
}

static void AppendJsonString(std::string& out, const std::string& value)
{
    out += "\"";
    out += JsonEscape(value.c_str());
    out += "\"";
}

static std::string HashToString(dmhash_t hash)
{
    const char* value = dmHashReverseSafe64(hash);
    if (value)
    {
        return value;
    }

    char buffer[32];
    dmSnPrintf(buffer, sizeof(buffer), "hash:%016llx", (unsigned long long)hash);
    return buffer;
}

static std::string ToHex64(uint64_t value)
{
    char buffer[32];
    dmSnPrintf(buffer, sizeof(buffer), "%016llx", (unsigned long long)value);
    return buffer;
}

static std::string UrlDecode(const char* value, size_t length)
{
    if (!value || length == 0)
    {
        return "";
    }

    std::string encoded(value, length);
    std::vector<char> decoded(encoded.size() + 1);
    dmURI::Decode(encoded.c_str(), decoded.data());
    return decoded.data();
}

static void ParseResource(const char* resource, std::string& path, std::vector<QueryParam>& query)
{
    path.clear();
    query.clear();

    if (!resource)
    {
        return;
    }

    const char* question = strchr(resource, '?');
    if (!question)
    {
        path = resource;
        return;
    }

    path.assign(resource, question - resource);
    const char* cursor = question + 1;
    while (*cursor)
    {
        const char* amp = strchr(cursor, '&');
        if (!amp)
        {
            amp = cursor + strlen(cursor);
        }

        const char* equals = (const char*)memchr(cursor, '=', amp - cursor);
        QueryParam param;
        if (equals)
        {
            param.m_Key = UrlDecode(cursor, equals - cursor);
            param.m_Value = UrlDecode(equals + 1, amp - equals - 1);
        }
        else
        {
            param.m_Key = UrlDecode(cursor, amp - cursor);
            param.m_Value = "";
        }
        if (!param.m_Key.empty())
        {
            query.push_back(param);
        }

        cursor = *amp ? amp + 1 : amp;
    }
}

static bool GetParam(const std::vector<QueryParam>& query, const char* key, std::string& value)
{
    for (uint32_t i = 0; i < query.size(); ++i)
    {
        if (query[i].m_Key == key)
        {
            value = query[i].m_Value;
            return true;
        }
    }
    return false;
}

static bool GetFloatParam(const std::vector<QueryParam>& query, const char* key, float& value)
{
    std::string text;
    if (!GetParam(query, key, text) || text.empty())
    {
        return false;
    }
    char* end = 0;
    double parsed = strtod(text.c_str(), &end);
    if (!end || *end != 0 || !isfinite(parsed))
    {
        return false;
    }
    value = (float)parsed;
    return true;
}

static bool GetBoolParam(const std::vector<QueryParam>& query, const char* key, bool& value)
{
    std::string text;
    if (!GetParam(query, key, text))
    {
        return false;
    }
    value = text == "1" || text == "true" || text == "yes";
    return true;
}

static bool ContainsCaseInsensitive(const std::string& haystack, const std::string& needle)
{
    if (needle.empty())
    {
        return true;
    }
    if (haystack.size() < needle.size())
    {
        return false;
    }
    for (size_t i = 0; i <= haystack.size() - needle.size(); ++i)
    {
        bool match = true;
        for (size_t j = 0; j < needle.size(); ++j)
        {
            if (tolower((unsigned char)haystack[i + j]) != tolower((unsigned char)needle[j]))
            {
                match = false;
                break;
            }
        }
        if (match)
        {
            return true;
        }
    }
    return false;
}

static void SendResponse(dmWebServer::Request* request, int status_code, const char* content_type, const std::string& response)
{
    dmWebServer::SetStatusCode(request, status_code);
    dmWebServer::SendAttribute(request, "Content-Type", content_type);
    dmWebServer::SendAttribute(request, "Cache-Control", "no-store");
    dmWebServer::Send(request, response.c_str(), (uint32_t)response.size());
}

static void SendJson(dmWebServer::Request* request, int status_code, const std::string& response)
{
    SendResponse(request, status_code, "application/json", response);
}

static void SendError(dmWebServer::Request* request, int status_code, const char* code, const char* message)
{
    std::string response = "{\"ok\":false,\"error\":{\"code\":";
    AppendJsonString(response, code);
    response += ",\"message\":";
    AppendJsonString(response, message);
    response += "}}\n";
    SendJson(request, status_code, response);
}

static bool DrainRequestBody(dmWebServer::Request* request)
{
    char buffer[256];
    uint32_t remaining = request->m_ContentLength;
    while (remaining > 0)
    {
        uint32_t received = 0;
        uint32_t to_read = remaining < sizeof(buffer) ? remaining : (uint32_t)sizeof(buffer);
        dmWebServer::Result result = dmWebServer::Receive(request, buffer, to_read, &received);
        if (result != dmWebServer::RESULT_OK || received == 0)
        {
            return false;
        }
        remaining -= received;
    }
    return true;
}

static bool IsMethod(dmWebServer::Request* request, const char* method)
{
    return request->m_Method && strcmp(request->m_Method, method) == 0;
}

struct IncludeOptions
{
    bool m_Bounds;
    bool m_Properties;
    bool m_Children;

    IncludeOptions()
    : m_Bounds(true)
    , m_Properties(false)
    , m_Children(false)
    {
    }
};

static bool IncludeHasToken(const std::string& include, const char* token)
{
    size_t token_len = strlen(token);
    size_t start = 0;
    while (start <= include.size())
    {
        size_t end = include.find(',', start);
        if (end == std::string::npos)
        {
            end = include.size();
        }
        size_t first = start;
        while (first < end && isspace((unsigned char)include[first])) ++first;
        size_t last = end;
        while (last > first && isspace((unsigned char)include[last - 1])) --last;
        if (last - first == token_len && strncmp(include.c_str() + first, token, token_len) == 0)
        {
            return true;
        }
        if (end == include.size())
        {
            break;
        }
        start = end + 1;
    }
    return false;
}

static IncludeOptions ParseInclude(const std::vector<QueryParam>& query, bool default_properties, bool default_children)
{
    IncludeOptions options;
    options.m_Properties = default_properties;
    options.m_Children = default_children;

    std::string include;
    if (!GetParam(query, "include", include) || include.empty())
    {
        return options;
    }

    bool all = IncludeHasToken(include, "all");
    options.m_Bounds = all || IncludeHasToken(include, "bounds");
    options.m_Properties = all || IncludeHasToken(include, "properties");
    options.m_Children = all || IncludeHasToken(include, "children");
    return options;
}

static const char* SceneNodeTypeToString(dmGameObject::SceneNodeType type)
{
    switch (type)
    {
    case dmGameObject::SCENE_NODE_TYPE_COLLECTION: return "collectionc";
    case dmGameObject::SCENE_NODE_TYPE_GAMEOBJECT: return "goc";
    case dmGameObject::SCENE_NODE_TYPE_COMPONENT: return "component";
    case dmGameObject::SCENE_NODE_TYPE_SUBCOMPONENT: return "subcomponent";
    default: return "node";
    }
}

static std::string KindFromType(dmGameObject::SceneNodeType scene_node_type, const std::string& type)
{
    if (StartsWith(type, "gui_node_"))
    {
        return "gui_node";
    }
    if (type == "goc" || scene_node_type == dmGameObject::SCENE_NODE_TYPE_GAMEOBJECT)
    {
        return "game_object";
    }
    if (type == "collectionc" || scene_node_type == dmGameObject::SCENE_NODE_TYPE_COLLECTION)
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

static void SetAnchorFromPivot(Node& node, const std::string& pivot)
{
    if (pivot == "PIVOT_NW") { node.m_AnchorX = 0.0f; node.m_AnchorY = 0.0f; }
    else if (pivot == "PIVOT_N") { node.m_AnchorX = 0.5f; node.m_AnchorY = 0.0f; }
    else if (pivot == "PIVOT_NE") { node.m_AnchorX = 1.0f; node.m_AnchorY = 0.0f; }
    else if (pivot == "PIVOT_W") { node.m_AnchorX = 0.0f; node.m_AnchorY = 0.5f; }
    else if (pivot == "PIVOT_CENTER") { node.m_AnchorX = 0.5f; node.m_AnchorY = 0.5f; }
    else if (pivot == "PIVOT_E") { node.m_AnchorX = 1.0f; node.m_AnchorY = 0.5f; }
    else if (pivot == "PIVOT_SW") { node.m_AnchorX = 0.0f; node.m_AnchorY = 1.0f; }
    else if (pivot == "PIVOT_S") { node.m_AnchorX = 0.5f; node.m_AnchorY = 1.0f; }
    else if (pivot == "PIVOT_SE") { node.m_AnchorX = 1.0f; node.m_AnchorY = 1.0f; }
}

static Property MakeProperty(const dmGameObject::SceneNodeProperty& source)
{
    Property property;
    property.m_Name = HashToString(source.m_NameHash);

    switch (source.m_Type)
    {
    case dmGameObject::SCENE_NODE_PROPERTY_TYPE_HASH:
        property.m_StringValue = HashToString(source.m_Value.m_Hash);
        property.m_Json = "\"";
        property.m_Json += JsonEscape(property.m_StringValue.c_str());
        property.m_Json += "\"";
        break;
    case dmGameObject::SCENE_NODE_PROPERTY_TYPE_NUMBER:
        AppendNumber(property.m_Json, source.m_Value.m_Number);
        break;
    case dmGameObject::SCENE_NODE_PROPERTY_TYPE_BOOLEAN:
        property.m_BoolValue = source.m_Value.m_Bool;
        property.m_HasBool = true;
        property.m_Json = source.m_Value.m_Bool ? "true" : "false";
        property.m_StringValue = source.m_Value.m_Bool ? "true" : "false";
        break;
    case dmGameObject::SCENE_NODE_PROPERTY_TYPE_URL:
        property.m_StringValue = source.m_Value.m_URL;
        property.m_Json = "\"";
        property.m_Json += JsonEscape(property.m_StringValue.c_str());
        property.m_Json += "\"";
        break;
    case dmGameObject::SCENE_NODE_PROPERTY_TYPE_TEXT:
        property.m_StringValue = source.m_Value.m_Text ? source.m_Value.m_Text : "";
        property.m_Json = "\"";
        property.m_Json += JsonEscape(property.m_StringValue.c_str());
        property.m_Json += "\"";
        break;
    case dmGameObject::SCENE_NODE_PROPERTY_TYPE_VECTOR3:
    case dmGameObject::SCENE_NODE_PROPERTY_TYPE_VECTOR4:
    case dmGameObject::SCENE_NODE_PROPERTY_TYPE_QUAT:
        property.m_HasVector = true;
        property.m_VectorCount = source.m_Type == dmGameObject::SCENE_NODE_PROPERTY_TYPE_VECTOR3 ? 3 : 4;
        property.m_Json = "[";
        for (uint32_t i = 0; i < property.m_VectorCount; ++i)
        {
            property.m_Vector[i] = source.m_Value.m_V4[i];
            if (i > 0)
            {
                property.m_Json += ",";
            }
            AppendNumber(property.m_Json, property.m_Vector[i]);
        }
        property.m_Json += "]";
        break;
    default:
        property.m_Json = "null";
        break;
    }

    return property;
}

static void ApplyPropertyToNode(Node& node, const Property& property)
{
    if (property.m_Name == "id" && !property.m_StringValue.empty())
    {
        node.m_Name = property.m_StringValue;
    }
    else if (property.m_Name == "type" && !property.m_StringValue.empty())
    {
        node.m_Type = property.m_StringValue;
    }
    else if (property.m_Name == "text" && !property.m_StringValue.empty())
    {
        node.m_Text = property.m_StringValue;
    }
    else if (property.m_Name == "url" && !property.m_StringValue.empty())
    {
        node.m_Url = property.m_StringValue;
    }
    else if (property.m_Name == "resource" && !property.m_StringValue.empty())
    {
        node.m_Resource = property.m_StringValue;
    }
    else if (property.m_Name == "visible" && property.m_HasBool)
    {
        node.m_Visible = property.m_BoolValue;
    }
    else if (property.m_Name == "enabled" && property.m_HasBool)
    {
        node.m_Enabled = property.m_BoolValue;
    }
    else if (property.m_Name == "pivot" && !property.m_StringValue.empty())
    {
        SetAnchorFromPivot(node, property.m_StringValue);
    }

    if (property.m_HasVector && property.m_VectorCount >= 2)
    {
        if (property.m_Name == "world_position" || (!node.m_HasPosition && property.m_Name == "position"))
        {
            node.m_HasPosition = true;
            node.m_Position[0] = property.m_Vector[0];
            node.m_Position[1] = property.m_Vector[1];
            node.m_Position[2] = property.m_VectorCount >= 3 ? property.m_Vector[2] : 0.0f;
        }
        else if (property.m_Name == "world_size" || (!node.m_HasSize && property.m_Name == "size"))
        {
            node.m_HasSize = true;
            node.m_Size[0] = property.m_Vector[0];
            node.m_Size[1] = property.m_Vector[1];
            node.m_Size[2] = property.m_VectorCount >= 3 ? property.m_Vector[2] : 0.0f;
        }
    }
}

static void ComputeBounds(Node& node, uint32_t screen_w, uint32_t screen_h, uint32_t backbuffer_w, uint32_t backbuffer_h)
{
    if (!node.m_HasPosition || screen_w == 0 || screen_h == 0)
    {
        return;
    }

    float width = node.m_HasSize ? fabsf(node.m_Size[0]) : 0.0f;
    float height = node.m_HasSize ? fabsf(node.m_Size[1]) : 0.0f;
    float x = node.m_Position[0];
    float y = node.m_Position[1];

    if (!IsFinite(x) || !IsFinite(y) || !IsFinite(width) || !IsFinite(height))
    {
        return;
    }

    if (node.m_Kind == "gui_node")
    {
        node.m_Bounds.m_X = x - width * node.m_AnchorX;
        node.m_Bounds.m_Y = (float)screen_h - (y + height * node.m_AnchorY);
        node.m_Bounds.m_W = width;
        node.m_Bounds.m_H = height;
        node.m_Bounds.m_CX = node.m_Bounds.m_X + width * 0.5f;
        node.m_Bounds.m_CY = node.m_Bounds.m_Y + height * 0.5f;
    }
    else
    {
        float source_w = backbuffer_w > 0 ? (float)backbuffer_w : (float)screen_w;
        float source_h = backbuffer_h > 0 ? (float)backbuffer_h : (float)screen_h;
        float scale_x = source_w > 0.0f ? (float)screen_w / source_w : 1.0f;
        float scale_y = source_h > 0.0f ? (float)screen_h / source_h : 1.0f;
        node.m_Bounds.m_CX = x * scale_x;
        node.m_Bounds.m_CY = (source_h - y) * scale_y;
        node.m_Bounds.m_W = width * scale_x;
        node.m_Bounds.m_H = height * scale_y;
        node.m_Bounds.m_X = node.m_Bounds.m_CX - node.m_Bounds.m_W * 0.5f;
        node.m_Bounds.m_Y = node.m_Bounds.m_CY - node.m_Bounds.m_H * 0.5f;
    }

    node.m_Bounds.m_NX = node.m_Bounds.m_CX / (float)screen_w;
    node.m_Bounds.m_NY = node.m_Bounds.m_CY / (float)screen_h;
    node.m_Bounds.m_Valid = true;
}

static int32_t BuildNode(Snapshot& snapshot, dmGameObject::SceneNode* scene_node, int32_t parent_index, const std::string& parent_path, uint32_t sibling_index)
{
    Node node;

    dmGameObject::SceneNodePropertyIterator pit = dmGameObject::TraverseIterateProperties(scene_node);
    while (dmGameObject::TraverseIteratePropertiesNext(&pit))
    {
        Property property = MakeProperty(pit.m_Property);
        ApplyPropertyToNode(node, property);
        node.m_Properties.push_back(property);
    }

    if (node.m_Type.empty())
    {
        node.m_Type = SceneNodeTypeToString(scene_node->m_Type);
    }
    node.m_Kind = KindFromType(scene_node->m_Type, node.m_Type);

    if (node.m_Name.empty())
    {
        char buffer[64];
        dmSnPrintf(buffer, sizeof(buffer), "%s_%u", node.m_Type.c_str(), sibling_index);
        node.m_Name = buffer;
    }

    std::string safe_segment = node.m_Name;
    if (safe_segment.empty())
    {
        safe_segment = node.m_Type;
    }
    char segment[128];
    dmSnPrintf(segment, sizeof(segment), "%s[%u]", safe_segment.c_str(), sibling_index);
    node.m_Path = parent_path.empty() ? std::string("/") + segment : parent_path + "/" + segment;
    uint64_t id_hash = dmHashBuffer64(node.m_Path.c_str(), (uint32_t)node.m_Path.size());
    node.m_Id = "n:";
    node.m_Id += ToHex64(id_hash);

    if (parent_index >= 0 && (uint32_t)parent_index < snapshot.m_Nodes.size())
    {
        node.m_Parent = snapshot.m_Nodes[parent_index].m_Id;
    }

    ComputeBounds(node, snapshot.m_WindowWidth, snapshot.m_WindowHeight, snapshot.m_BackbufferWidth, snapshot.m_BackbufferHeight);

    uint32_t index = (uint32_t)snapshot.m_Nodes.size();
    snapshot.m_Nodes.push_back(node);

    dmGameObject::SceneNodeIterator it = dmGameObject::TraverseIterateChildren(scene_node);
    uint32_t child_index = 0;
    while (dmGameObject::TraverseIterateNext(&it))
    {
        int32_t child = BuildNode(snapshot, &it.m_Node, (int32_t)index, snapshot.m_Nodes[index].m_Path, child_index++);
        snapshot.m_Nodes[index].m_Children.push_back((uint32_t)child);
    }

    return (int32_t)index;
}

static void RefreshScreenInfo(Snapshot& snapshot)
{
    if (!g_Agent.m_GraphicsContext)
    {
        return;
    }

    snapshot.m_WindowWidth = dmGraphics::GetWindowWidth(g_Agent.m_GraphicsContext);
    snapshot.m_WindowHeight = dmGraphics::GetWindowHeight(g_Agent.m_GraphicsContext);
    snapshot.m_BackbufferWidth = dmGraphics::GetWidth(g_Agent.m_GraphicsContext);
    snapshot.m_BackbufferHeight = dmGraphics::GetHeight(g_Agent.m_GraphicsContext);
    dmGraphics::GetViewport(g_Agent.m_GraphicsContext, &snapshot.m_ViewportX, &snapshot.m_ViewportY, &snapshot.m_ViewportWidth, &snapshot.m_ViewportHeight);

    if (snapshot.m_WindowWidth == 0)
    {
        snapshot.m_WindowWidth = snapshot.m_ViewportWidth;
    }
    if (snapshot.m_WindowHeight == 0)
    {
        snapshot.m_WindowHeight = snapshot.m_ViewportHeight;
    }
}

static void UpdateSnapshot()
{
    Snapshot snapshot;
    snapshot.m_Sequence = g_Agent.m_Snapshot.m_Sequence + 1;
    RefreshScreenInfo(snapshot);

    if (g_Agent.m_Register)
    {
        dmGameObject::SceneNode root;
        if (dmGameObject::TraverseGetRoot(g_Agent.m_Register, &root))
        {
            snapshot.m_Root = BuildNode(snapshot, &root, -1, "", 0);
        }
    }

    g_Agent.m_Snapshot = snapshot;
}

static const Node* FindNodeById(const std::string& id)
{
    const Snapshot& snapshot = g_Agent.m_Snapshot;
    for (uint32_t i = 0; i < snapshot.m_Nodes.size(); ++i)
    {
        if (snapshot.m_Nodes[i].m_Id == id)
        {
            return &snapshot.m_Nodes[i];
        }
    }
    return 0;
}

static void AppendBoundsJson(std::string& out, const Bounds& bounds)
{
    if (!bounds.m_Valid)
    {
        out += "null";
        return;
    }

    out += "{\"screen\":{\"x\":";
    AppendNumber(out, bounds.m_X);
    out += ",\"y\":";
    AppendNumber(out, bounds.m_Y);
    out += ",\"w\":";
    AppendNumber(out, bounds.m_W);
    out += ",\"h\":";
    AppendNumber(out, bounds.m_H);
    out += "},\"center\":{\"x\":";
    AppendNumber(out, bounds.m_CX);
    out += ",\"y\":";
    AppendNumber(out, bounds.m_CY);
    out += "},\"normalized\":{\"x\":";
    AppendNumber(out, bounds.m_NX);
    out += ",\"y\":";
    AppendNumber(out, bounds.m_NY);
    out += "}}";
}

static void AppendNodeJson(std::string& out, const Snapshot& snapshot, const Node& node, const IncludeOptions& include, bool recursive, bool visible_only)
{
    out += "{\"id\":";
    AppendJsonString(out, node.m_Id);
    out += ",\"name\":";
    AppendJsonString(out, node.m_Name);
    out += ",\"type\":";
    AppendJsonString(out, node.m_Type);
    out += ",\"kind\":";
    AppendJsonString(out, node.m_Kind);
    out += ",\"path\":";
    AppendJsonString(out, node.m_Path);
    out += ",\"parent\":";
    if (node.m_Parent.empty())
    {
        out += "null";
    }
    else
    {
        AppendJsonString(out, node.m_Parent);
    }
    out += ",\"visible\":";
    out += node.m_Visible ? "true" : "false";
    out += ",\"enabled\":";
    out += node.m_Enabled ? "true" : "false";

    if (!node.m_Text.empty())
    {
        out += ",\"text\":";
        AppendJsonString(out, node.m_Text);
    }
    if (!node.m_Url.empty())
    {
        out += ",\"url\":";
        AppendJsonString(out, node.m_Url);
    }
    if (!node.m_Resource.empty())
    {
        out += ",\"resource\":";
        AppendJsonString(out, node.m_Resource);
    }

    if (include.m_Bounds)
    {
        out += ",\"bounds\":";
        AppendBoundsJson(out, node.m_Bounds);
    }

    if (include.m_Properties)
    {
        out += ",\"properties\":{";
        for (uint32_t i = 0; i < node.m_Properties.size(); ++i)
        {
            if (i > 0)
            {
                out += ",";
            }
            AppendJsonString(out, node.m_Properties[i].m_Name);
            out += ":";
            out += node.m_Properties[i].m_Json;
        }
        out += "}";
    }

    if (include.m_Children || recursive)
    {
        out += ",\"children\":[";
        uint32_t emitted_children = 0;
        for (uint32_t i = 0; i < node.m_Children.size(); ++i)
        {
            uint32_t child = node.m_Children[i];
            if (child < snapshot.m_Nodes.size())
            {
                const Node& child_node = snapshot.m_Nodes[child];
                if (visible_only && !child_node.m_Visible)
                {
                    continue;
                }
                if (emitted_children > 0)
                {
                    out += ",";
                }
                AppendNodeJson(out, snapshot, child_node, include, recursive, visible_only);
                ++emitted_children;
            }
        }
        out += "]";
    }

    out += "}";
}

static void AppendScreenJson(std::string& out, const Snapshot& snapshot)
{
    out += "{\"window\":{\"width\":";
    AppendNumber(out, snapshot.m_WindowWidth);
    out += ",\"height\":";
    AppendNumber(out, snapshot.m_WindowHeight);
    out += "},\"backbuffer\":{\"width\":";
    AppendNumber(out, snapshot.m_BackbufferWidth);
    out += ",\"height\":";
    AppendNumber(out, snapshot.m_BackbufferHeight);
    out += "},\"viewport\":{\"x\":";
    AppendNumber(out, snapshot.m_ViewportX);
    out += ",\"y\":";
    AppendNumber(out, snapshot.m_ViewportY);
    out += ",\"width\":";
    AppendNumber(out, snapshot.m_ViewportWidth);
    out += ",\"height\":";
    AppendNumber(out, snapshot.m_ViewportHeight);
    out += "},\"coordinates\":{\"origin\":\"top-left\",\"units\":\"screen_pixels\"}}";
}

static bool IsScreenshotSupported()
{
    if (!g_Agent.m_GraphicsContext)
    {
        return false;
    }

    dmGraphics::AdapterFamily family = dmGraphics::GetInstalledAdapterFamily();
    return family == dmGraphics::ADAPTER_FAMILY_OPENGL ||
           family == dmGraphics::ADAPTER_FAMILY_OPENGLES ||
           family == dmGraphics::ADAPTER_FAMILY_VULKAN;
}

static void HandleHealth(dmWebServer::Request* request)
{
    std::string response = "{\"ok\":true,\"data\":{\"version\":";
    AppendJsonString(response, API_VERSION);
    response += ",\"debug\":true,\"platform\":";
#if defined(DM_PLATFORM_OSX)
    AppendJsonString(response, "macos");
#elif defined(DM_PLATFORM_WINDOWS)
    AppendJsonString(response, "windows");
#elif defined(DM_PLATFORM_LINUX)
    AppendJsonString(response, "linux");
#elif defined(DM_PLATFORM_ANDROID)
    AppendJsonString(response, "android");
#elif defined(DM_PLATFORM_IOS)
    AppendJsonString(response, "ios");
#else
    AppendJsonString(response, "unknown");
#endif
    response += ",\"capabilities\":[\"scene\",\"nodes\",\"node\",\"input.click\",\"input.drag\",\"input.key\"";
    if (IsScreenshotSupported())
    {
        response += ",\"screenshot\"";
    }
    response += "],\"screen\":";
    AppendScreenJson(response, g_Agent.m_Snapshot);
    response += ",\"scene_sequence\":";
    AppendNumber(response, (double)g_Agent.m_Snapshot.m_Sequence);
    response += "}}\n";
    SendJson(request, 200, response);
}

static void HandleScreen(dmWebServer::Request* request)
{
    std::string response = "{\"ok\":true,\"data\":";
    AppendScreenJson(response, g_Agent.m_Snapshot);
    response += "}\n";
    SendJson(request, 200, response);
}

static void HandleScene(dmWebServer::Request* request, const std::vector<QueryParam>& query)
{
    IncludeOptions include = ParseInclude(query, false, true);
    bool visible_only = false;
    GetBoolParam(query, "visible", visible_only);

    const Snapshot& snapshot = g_Agent.m_Snapshot;
    if (snapshot.m_Root < 0 || (uint32_t)snapshot.m_Root >= snapshot.m_Nodes.size())
    {
        SendError(request, 404, "scene_unavailable", "scene graph is not available");
        return;
    }

    std::string response = "{\"ok\":true,\"data\":{\"count\":";
    AppendNumber(response, (double)snapshot.m_Nodes.size());
    response += ",\"visible_filter\":";
    response += visible_only ? "true" : "false";
    response += ",\"screen\":";
    AppendScreenJson(response, snapshot);
    response += ",\"root\":";
    AppendNodeJson(response, snapshot, snapshot.m_Nodes[snapshot.m_Root], include, true, visible_only);
    response += "}}\n";
    SendJson(request, 200, response);
}

static bool NodeMatchesFilters(const Node& node, const std::vector<QueryParam>& query)
{
    std::string value;
    if (GetParam(query, "id", value) && node.m_Id != value)
    {
        return false;
    }
    if (GetParam(query, "type", value) && !ContainsCaseInsensitive(node.m_Type, value))
    {
        return false;
    }
    if (GetParam(query, "name", value) && !ContainsCaseInsensitive(node.m_Name, value))
    {
        return false;
    }
    if (GetParam(query, "text", value) && !ContainsCaseInsensitive(node.m_Text, value))
    {
        return false;
    }
    if (GetParam(query, "url", value) && !ContainsCaseInsensitive(node.m_Url, value))
    {
        return false;
    }
    bool visible = false;
    if (GetBoolParam(query, "visible", visible) && node.m_Visible != visible)
    {
        return false;
    }
    return true;
}

static void HandleNodes(dmWebServer::Request* request, const std::vector<QueryParam>& query)
{
    IncludeOptions include = ParseInclude(query, false, false);
    float limit_f = 50.0f;
    GetFloatParam(query, "limit", limit_f);
    uint32_t limit = (uint32_t)std::max(0.0f, std::min(limit_f, 500.0f));

    const Snapshot& snapshot = g_Agent.m_Snapshot;
    uint32_t matched = 0;
    uint32_t emitted = 0;
    std::string response = "{\"ok\":true,\"data\":{\"nodes\":[";
    for (uint32_t i = 0; i < snapshot.m_Nodes.size(); ++i)
    {
        const Node& node = snapshot.m_Nodes[i];
        if (!NodeMatchesFilters(node, query))
        {
            continue;
        }
        ++matched;
        if (emitted >= limit)
        {
            continue;
        }
        if (emitted > 0)
        {
            response += ",";
        }
        AppendNodeJson(response, snapshot, node, include, false, false);
        ++emitted;
    }
    response += "],\"count\":";
    AppendNumber(response, (double)emitted);
    response += ",\"matched\":";
    AppendNumber(response, (double)matched);
    response += ",\"total\":";
    AppendNumber(response, (double)snapshot.m_Nodes.size());
    response += "}}\n";
    SendJson(request, 200, response);
}

static void HandleNode(dmWebServer::Request* request, const std::vector<QueryParam>& query)
{
    std::string id;
    if (!GetParam(query, "id", id) || id.empty())
    {
        SendError(request, 400, "bad_request", "missing node id");
        return;
    }

    const Node* node = FindNodeById(id);
    if (!node)
    {
        SendError(request, 404, "not_found", "node id was not found");
        return;
    }

    IncludeOptions include = ParseInclude(query, true, false);
    std::string response = "{\"ok\":true,\"data\":{\"node\":";
    AppendNodeJson(response, g_Agent.m_Snapshot, *node, include, include.m_Children, false);
    response += "}}\n";
    SendJson(request, 200, response);
}

static bool CanQueueInputEvent()
{
    return g_Agent.m_InputEvents.size() < MAX_INPUT_EVENTS;
}

static bool AddMouseInput(float x1, float y1, float x2, float y2, float duration)
{
    if (!CanQueueInputEvent())
    {
        return false;
    }

    InputEvent event;
    event.m_Type = INPUT_EVENT_MOUSE;
    event.m_X1 = x1;
    event.m_Y1 = y1;
    event.m_X2 = x2;
    event.m_Y2 = y2;
    event.m_Duration = std::max(0.0f, duration);
    event.m_MouseButton = dmHID::MOUSE_BUTTON_LEFT;
    g_Agent.m_InputEvents.push_back(event);
    return true;
}

static bool AddKeyInput(const std::string& keys)
{
    if (keys.empty() || !CanQueueInputEvent())
    {
        return false;
    }

    InputEvent event;
    event.m_Type = INPUT_EVENT_KEYS;
    event.m_Keys = keys;
    g_Agent.m_InputEvents.push_back(event);
    return true;
}

static bool GetNodeCenter(const std::string& id, float& x, float& y, std::string& error)
{
    const Node* node = FindNodeById(id);
    if (!node)
    {
        error = "node id was not found";
        return false;
    }
    if (!node->m_Bounds.m_Valid)
    {
        error = "node has no screen bounds";
        return false;
    }
    x = node->m_Bounds.m_CX;
    y = node->m_Bounds.m_CY;
    return true;
}

static void HandleClick(dmWebServer::Request* request, const std::vector<QueryParam>& query)
{
    float x = 0.0f;
    float y = 0.0f;
    std::string id;
    if (GetParam(query, "id", id) && !id.empty())
    {
        std::string error;
        if (!GetNodeCenter(id, x, y, error))
        {
            SendError(request, 404, "not_found", error.c_str());
            return;
        }
    }
    else if (!GetFloatParam(query, "x", x) || !GetFloatParam(query, "y", y))
    {
        SendError(request, 400, "bad_request", "provide either id or x/y");
        return;
    }

    if (!AddMouseInput(x, y, x, y, 0.0f))
    {
        SendError(request, 429, "input_queue_full", "too many input events are already queued");
        return;
    }

    std::string response = "{\"ok\":true,\"data\":{\"queued\":\"click\",\"x\":";
    AppendNumber(response, x);
    response += ",\"y\":";
    AppendNumber(response, y);
    response += "}}\n";
    SendJson(request, 200, response);
}

static void HandleDrag(dmWebServer::Request* request, const std::vector<QueryParam>& query)
{
    float x1 = 0.0f;
    float y1 = 0.0f;
    float x2 = 0.0f;
    float y2 = 0.0f;
    float duration = 0.35f;
    GetFloatParam(query, "duration", duration);

    std::string from_id;
    std::string to_id;
    if (GetParam(query, "from_id", from_id) && GetParam(query, "to_id", to_id) && !from_id.empty() && !to_id.empty())
    {
        std::string error;
        if (!GetNodeCenter(from_id, x1, y1, error))
        {
            SendError(request, 404, "not_found", error.c_str());
            return;
        }
        if (!GetNodeCenter(to_id, x2, y2, error))
        {
            SendError(request, 404, "not_found", error.c_str());
            return;
        }
    }
    else if (!GetFloatParam(query, "x1", x1) || !GetFloatParam(query, "y1", y1) || !GetFloatParam(query, "x2", x2) || !GetFloatParam(query, "y2", y2))
    {
        SendError(request, 400, "bad_request", "provide either from_id/to_id or x1/y1/x2/y2");
        return;
    }

    if (!AddMouseInput(x1, y1, x2, y2, std::max(0.0f, duration)))
    {
        SendError(request, 429, "input_queue_full", "too many input events are already queued");
        return;
    }

    std::string response = "{\"ok\":true,\"data\":{\"queued\":\"drag\",\"from\":{\"x\":";
    AppendNumber(response, x1);
    response += ",\"y\":";
    AppendNumber(response, y1);
    response += "},\"to\":{\"x\":";
    AppendNumber(response, x2);
    response += ",\"y\":";
    AppendNumber(response, y2);
    response += "},\"duration\":";
    AppendNumber(response, duration);
    response += "}}\n";
    SendJson(request, 200, response);
}

static void HandleKey(dmWebServer::Request* request, const std::vector<QueryParam>& query)
{
    std::string value;
    if (!GetParam(query, "keys", value))
    {
        GetParam(query, "text", value);
    }
    if (value.empty())
    {
        SendError(request, 400, "bad_request", "provide text or keys");
        return;
    }
    if (value.size() > MAX_KEY_INPUT_BYTES)
    {
        SendError(request, 413, "input_too_large", "text or keys parameter is too large");
        return;
    }

    if (!AddKeyInput(value))
    {
        SendError(request, 429, "input_queue_full", "too many input events are already queued");
        return;
    }

    std::string response = "{\"ok\":true,\"data\":{\"queued\":\"key\",\"length\":";
    AppendNumber(response, (double)value.size());
    response += "}}\n";
    SendJson(request, 200, response);
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
    int written = dmSnPrintf(directory, directory_size, "%s%sdefold-agent", temp_dir, separator);
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
        dmLogError("Unable to create agent screenshot directory '%s' (%d)", directory, errno);
        return false;
    }
    return true;
}

static bool BuildScreenshotPath(char* path, uint32_t path_size)
{
    char directory[1024];
    if (!EnsureScreenshotDirectory(directory, sizeof(directory)))
    {
        return false;
    }

    uint32_t timestamp = (uint32_t)(dmTime::GetTime() / 1000000);
    uint32_t counter = ++g_Agent.m_ScreenshotCounter;
    int written = dmSnPrintf(path, path_size, "%s/screenshot-%u-%u.png", directory, timestamp, counter);
    if (written < 0 || (uint32_t)written >= path_size)
    {
        return false;
    }
    NormalizeSlashes(path);
    return true;
}

static bool ScheduleScreenshot(const char* path)
{
    if (g_Agent.m_ScreenshotPending)
    {
        return false;
    }
    int written = dmSnPrintf(g_Agent.m_ScreenshotPath, sizeof(g_Agent.m_ScreenshotPath), "%s", path);
    if (written < 0 || (uint32_t)written >= sizeof(g_Agent.m_ScreenshotPath))
    {
        return false;
    }
    g_Agent.m_ScreenshotPending = true;
    return true;
}

static void WriteBE32(std::vector<uint8_t>& out, uint32_t value)
{
    out.push_back((uint8_t)((value >> 24) & 0xff));
    out.push_back((uint8_t)((value >> 16) & 0xff));
    out.push_back((uint8_t)((value >> 8) & 0xff));
    out.push_back((uint8_t)(value & 0xff));
}

static uint32_t Crc32(const uint8_t* data, size_t size)
{
    uint32_t crc = 0xffffffffU;
    for (size_t i = 0; i < size; ++i)
    {
        crc ^= data[i];
        for (uint32_t bit = 0; bit < 8; ++bit)
        {
            crc = (crc >> 1) ^ (0xedb88320U & (uint32_t)-(int32_t)(crc & 1));
        }
    }
    return crc ^ 0xffffffffU;
}

static uint32_t Adler32(const uint8_t* data, size_t size)
{
    uint32_t a = 1;
    uint32_t b = 0;
    for (size_t i = 0; i < size; ++i)
    {
        a = (a + data[i]) % 65521U;
        b = (b + a) % 65521U;
    }
    return (b << 16) | a;
}

static void AppendChunk(std::vector<uint8_t>& out, const char type[4], const std::vector<uint8_t>& data)
{
    WriteBE32(out, (uint32_t)data.size());
    size_t type_offset = out.size();
    out.push_back((uint8_t)type[0]);
    out.push_back((uint8_t)type[1]);
    out.push_back((uint8_t)type[2]);
    out.push_back((uint8_t)type[3]);
    out.insert(out.end(), data.begin(), data.end());
    uint32_t crc = Crc32(&out[type_offset], 4 + data.size());
    WriteBE32(out, crc);
}

static bool EncodePngRgba(const std::vector<uint8_t>& rgba, uint32_t width, uint32_t height, std::vector<uint8_t>& png)
{
    if (rgba.size() != (size_t)width * height * 4 || width == 0 || height == 0)
    {
        return false;
    }

    std::vector<uint8_t> raw;
    raw.reserve(((size_t)width * 4 + 1) * height);
    for (uint32_t y = 0; y < height; ++y)
    {
        raw.push_back(0);
        const uint8_t* row = &rgba[(size_t)y * width * 4];
        raw.insert(raw.end(), row, row + (size_t)width * 4);
    }

    std::vector<uint8_t> zlib;
    zlib.reserve(raw.size() + raw.size() / 65535 * 5 + 16);
    zlib.push_back(0x78);
    zlib.push_back(0x01);

    size_t offset = 0;
    while (offset < raw.size())
    {
        uint32_t block_size = (uint32_t)std::min((size_t)65535, raw.size() - offset);
        bool final_block = offset + block_size == raw.size();
        zlib.push_back(final_block ? 1 : 0);
        zlib.push_back((uint8_t)(block_size & 0xff));
        zlib.push_back((uint8_t)((block_size >> 8) & 0xff));
        uint16_t nlen = (uint16_t)~block_size;
        zlib.push_back((uint8_t)(nlen & 0xff));
        zlib.push_back((uint8_t)((nlen >> 8) & 0xff));
        zlib.insert(zlib.end(), raw.begin() + offset, raw.begin() + offset + block_size);
        offset += block_size;
    }
    WriteBE32(zlib, Adler32(raw.data(), raw.size()));

    png.clear();
    const uint8_t signature[] = { 137, 80, 78, 71, 13, 10, 26, 10 };
    png.insert(png.end(), signature, signature + sizeof(signature));

    std::vector<uint8_t> ihdr;
    WriteBE32(ihdr, width);
    WriteBE32(ihdr, height);
    ihdr.push_back(8);
    ihdr.push_back(6);
    ihdr.push_back(0);
    ihdr.push_back(0);
    ihdr.push_back(0);
    AppendChunk(png, "IHDR", ihdr);
    AppendChunk(png, "IDAT", zlib);
    std::vector<uint8_t> empty;
    AppendChunk(png, "IEND", empty);
    return true;
}

static bool CaptureScreenshotPng(std::vector<uint8_t>& png)
{
    if (!IsScreenshotSupported())
    {
        return false;
    }

    int32_t x = 0;
    int32_t y = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    dmGraphics::GetViewport(g_Agent.m_GraphicsContext, &x, &y, &width, &height);
    if (width == 0 || height == 0)
    {
        return false;
    }

    std::vector<uint8_t> pixels((size_t)width * height * 4);
    dmGraphics::ReadPixels(g_Agent.m_GraphicsContext, x, y, width, height, pixels.data(), (uint32_t)pixels.size());

    for (size_t i = 0; i < pixels.size(); i += 4)
    {
        uint8_t b = pixels[i + 0];
        pixels[i + 0] = pixels[i + 2];
        pixels[i + 2] = b;
    }

    return EncodePngRgba(pixels, width, height, png);
}

static bool WriteBytesToFile(const char* path, const std::vector<uint8_t>& bytes)
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
    size_t written = fwrite(bytes.data(), 1, bytes.size(), file);
    bool close_ok = fclose(file) == 0;
    return written == bytes.size() && close_ok;
}

static void ProcessPendingScreenshot()
{
    if (!g_Agent.m_ScreenshotPending)
    {
        return;
    }

    char path[1024];
    dmSnPrintf(path, sizeof(path), "%s", g_Agent.m_ScreenshotPath);
    g_Agent.m_ScreenshotPending = false;

    std::vector<uint8_t> png;
    if (!CaptureScreenshotPng(png))
    {
        dmLogError("Unable to capture agent screenshot");
        return;
    }
    if (!WriteBytesToFile(path, png))
    {
        dmLogError("Unable to write agent screenshot '%s'", path);
    }
}

static void HandleScreenshot(dmWebServer::Request* request)
{
    if (!IsScreenshotSupported())
    {
        SendError(request, 501, "screenshot_unsupported", "screenshot capture is not supported by the current graphics adapter");
        return;
    }

    char path[1024];
    if (!BuildScreenshotPath(path, sizeof(path)))
    {
        SendError(request, 500, "screenshot_path_failed", "failed to create screenshot path");
        return;
    }
    if (!ScheduleScreenshot(path))
    {
        SendError(request, 409, "screenshot_pending", "a screenshot is already pending");
        return;
    }

    std::string response = "{\"ok\":true,\"data\":{\"path\":";
    AppendJsonString(response, path);
    response += ",\"pending\":true}}\n";
    SendJson(request, 200, response);
}

static dmHID::Key KeyFromName(const std::string& name)
{
    struct KeyMap { const char* m_Name; dmHID::Key m_Key; };
    static const KeyMap keys[] = {
        {"KEY_SPACE", dmHID::KEY_SPACE},
        {"KEY_ESC", dmHID::KEY_ESC},
        {"KEY_ESCAPE", dmHID::KEY_ESC},
        {"KEY_UP", dmHID::KEY_UP},
        {"KEY_DOWN", dmHID::KEY_DOWN},
        {"KEY_LEFT", dmHID::KEY_LEFT},
        {"KEY_RIGHT", dmHID::KEY_RIGHT},
        {"KEY_TAB", dmHID::KEY_TAB},
        {"KEY_ENTER", dmHID::KEY_ENTER},
        {"KEY_BACKSPACE", dmHID::KEY_BACKSPACE},
        {"KEY_INSERT", dmHID::KEY_INSERT},
        {"KEY_DEL", dmHID::KEY_DEL},
        {"KEY_DELETE", dmHID::KEY_DEL},
        {"KEY_PAGEUP", dmHID::KEY_PAGEUP},
        {"KEY_PAGEDOWN", dmHID::KEY_PAGEDOWN},
        {"KEY_HOME", dmHID::KEY_HOME},
        {"KEY_END", dmHID::KEY_END},
        {"KEY_LSHIFT", dmHID::KEY_LSHIFT},
        {"KEY_RSHIFT", dmHID::KEY_RSHIFT},
        {"KEY_LCTRL", dmHID::KEY_LCTRL},
        {"KEY_RCTRL", dmHID::KEY_RCTRL},
        {"KEY_LALT", dmHID::KEY_LALT},
        {"KEY_RALT", dmHID::KEY_RALT},
        {"KEY_F1", dmHID::KEY_F1},
        {"KEY_F2", dmHID::KEY_F2},
        {"KEY_F3", dmHID::KEY_F3},
        {"KEY_F4", dmHID::KEY_F4},
        {"KEY_F5", dmHID::KEY_F5},
        {"KEY_F6", dmHID::KEY_F6},
        {"KEY_F7", dmHID::KEY_F7},
        {"KEY_F8", dmHID::KEY_F8},
        {"KEY_F9", dmHID::KEY_F9},
        {"KEY_F10", dmHID::KEY_F10},
        {"KEY_F11", dmHID::KEY_F11},
        {"KEY_F12", dmHID::KEY_F12}
    };

    for (uint32_t i = 0; i < DM_ARRAY_SIZE(keys); ++i)
    {
        if (name == keys[i].m_Name)
        {
            return keys[i].m_Key;
        }
    }

    if (name.size() == 5 && StartsWith(name, "KEY_") && name[4] >= 'A' && name[4] <= 'Z')
    {
        return (dmHID::Key)(dmHID::KEY_A + (name[4] - 'A'));
    }
    if (name.size() == 5 && StartsWith(name, "KEY_") && name[4] >= '0' && name[4] <= '9')
    {
        return (dmHID::Key)(dmHID::KEY_0 + (name[4] - '0'));
    }
    return dmHID::MAX_KEY_COUNT;
}

static void DoTouch(dmHID::HMouse mouse, dmHID::HTouchDevice device, int32_t x, int32_t y, bool pressed, bool released)
{
    if (device != dmHID::INVALID_TOUCH_DEVICE_HANDLE)
    {
        dmHID::Phase phase = dmHID::PHASE_MOVED;
        if (pressed)
        {
            phase = dmHID::PHASE_BEGAN;
        }
        if (released)
        {
            phase = dmHID::PHASE_ENDED;
        }
        dmHID::AddTouch(device, x, y, 0, phase);
    }

    if (mouse != dmHID::INVALID_MOUSE_HANDLE)
    {
        dmHID::SetMouseButton(mouse, dmHID::MOUSE_BUTTON_LEFT, released ? false : true);
        dmHID::SetMousePosition(mouse, x, y);
    }
}

static bool UpdateMouseEvent(dmHID::HMouse mouse, dmHID::HTouchDevice device, float dt, InputEvent& event)
{
    float t = event.m_Duration > 0.0f ? event.m_Elapsed / event.m_Duration : 0.0f;
    t = std::max(0.0f, std::min(t, 1.0f));
    float x = event.m_X1 + (event.m_X2 - event.m_X1) * t;
    float y = event.m_Y1 + (event.m_Y2 - event.m_Y1) * t;

    bool pressed = event.m_Elapsed == 0.0f;
    bool released = event.m_Elapsed >= event.m_Duration;
    event.m_Elapsed += dt;

    if (pressed && released)
    {
        released = false;
    }

    DoTouch(mouse, device, (int32_t)x, (int32_t)y, pressed, released);
    return released;
}

static bool ParseSpecialKey(const std::string& keys, uint32_t index, std::string& name, uint32_t& next_index)
{
    if (index >= keys.size() || keys[index] != '{')
    {
        return false;
    }
    size_t end = keys.find('}', index + 1);
    if (end == std::string::npos)
    {
        return false;
    }
    name.assign(keys.begin() + index + 1, keys.begin() + end);
    next_index = (uint32_t)end + 1;
    return !name.empty();
}

static bool UpdateKeyEvent(dmHID::HContext context, dmHID::HKeyboard keyboard, InputEvent& event)
{
    if (event.m_ActiveKey != dmHID::MAX_KEY_COUNT)
    {
        if (keyboard != dmHID::INVALID_KEYBOARD_HANDLE)
        {
            dmHID::SetKey(keyboard, event.m_ActiveKey, false);
        }
        event.m_ActiveKey = dmHID::MAX_KEY_COUNT;
        return event.m_KeyIndex >= event.m_Keys.size();
    }

    if (event.m_KeyIndex >= event.m_Keys.size())
    {
        return true;
    }

    std::string key_name;
    uint32_t next_index = event.m_KeyIndex;
    if (ParseSpecialKey(event.m_Keys, event.m_KeyIndex, key_name, next_index))
    {
        dmHID::Key key = KeyFromName(key_name);
        event.m_KeyIndex = next_index;
        if (key != dmHID::MAX_KEY_COUNT && keyboard != dmHID::INVALID_KEYBOARD_HANDLE)
        {
            dmHID::SetKey(keyboard, key, true);
            event.m_ActiveKey = key;
            return false;
        }
        return event.m_KeyIndex >= event.m_Keys.size();
    }

    const char* cursor = event.m_Keys.c_str() + event.m_KeyIndex;
    uint32_t codepoint = dmUtf8::NextChar(&cursor);
    event.m_KeyIndex = (uint32_t)(cursor - event.m_Keys.c_str());
    if (codepoint != 0)
    {
        dmHID::AddKeyboardChar(context, (int)codepoint);
    }
    return event.m_KeyIndex >= event.m_Keys.size();
}

static void UpdateInput(float dt)
{
    if (!g_Agent.m_HidContext)
    {
        return;
    }

    dmHID::HMouse mouse = dmHID::GetMouse(g_Agent.m_HidContext, 0);
    dmHID::HKeyboard keyboard = dmHID::GetKeyboard(g_Agent.m_HidContext, 0);
    dmHID::HTouchDevice touch_device = dmHID::INVALID_TOUCH_DEVICE_HANDLE;
#if defined(DM_PLATFORM_IOS) || defined(DM_PLATFORM_ANDROID) || defined(DM_PLATFORM_SWITCH)
    touch_device = dmHID::GetTouchDevice(g_Agent.m_HidContext, 0);
#endif

    for (uint32_t i = 0; i < g_Agent.m_InputEvents.size();)
    {
        bool done = false;
        InputEvent& event = g_Agent.m_InputEvents[i];
        if (event.m_Type == INPUT_EVENT_MOUSE)
        {
            done = UpdateMouseEvent(mouse, touch_device, dt, event);
        }
        else if (event.m_Type == INPUT_EVENT_KEYS)
        {
            done = UpdateKeyEvent(g_Agent.m_HidContext, keyboard, event);
        }

        if (done)
        {
            g_Agent.m_InputEvents.erase(g_Agent.m_InputEvents.begin() + i);
        }
        else
        {
            ++i;
        }
    }
}

static void AgentHandler(void* user_data, dmWebServer::Request* request)
{
    (void)user_data;

    if (request->m_ContentLength > 0)
    {
        DrainRequestBody(request);
        SendError(request, 400, "body_not_supported", "request body is not supported; use query parameters");
        return;
    }

    std::string path;
    std::vector<QueryParam> query;
    ParseResource(request->m_Resource, path, query);

    if (!StartsWith(path, API_PREFIX))
    {
        SendError(request, 404, "not_found", "endpoint not found");
        return;
    }

    std::string route = path.substr(strlen(API_PREFIX));
    if (route.empty())
    {
        route = "/";
    }

    if (route == "/health")
    {
        if (!IsMethod(request, "GET")) { SendError(request, 405, "method_not_allowed", "use GET"); return; }
        HandleHealth(request);
    }
    else if (route == "/screen")
    {
        if (!IsMethod(request, "GET")) { SendError(request, 405, "method_not_allowed", "use GET"); return; }
        HandleScreen(request);
    }
    else if (route == "/scene")
    {
        if (!IsMethod(request, "GET")) { SendError(request, 405, "method_not_allowed", "use GET"); return; }
        HandleScene(request, query);
    }
    else if (route == "/nodes")
    {
        if (!IsMethod(request, "GET")) { SendError(request, 405, "method_not_allowed", "use GET"); return; }
        HandleNodes(request, query);
    }
    else if (route == "/node")
    {
        if (!IsMethod(request, "GET")) { SendError(request, 405, "method_not_allowed", "use GET"); return; }
        HandleNode(request, query);
    }
    else if (route == "/input/click")
    {
        if (!IsMethod(request, "POST")) { SendError(request, 405, "method_not_allowed", "use POST"); return; }
        HandleClick(request, query);
    }
    else if (route == "/input/drag")
    {
        if (!IsMethod(request, "POST")) { SendError(request, 405, "method_not_allowed", "use POST"); return; }
        HandleDrag(request, query);
    }
    else if (route == "/input/key")
    {
        if (!IsMethod(request, "POST")) { SendError(request, 405, "method_not_allowed", "use POST"); return; }
        HandleKey(request, query);
    }
    else if (route == "/screenshot")
    {
        if (!IsMethod(request, "GET")) { SendError(request, 405, "method_not_allowed", "use GET"); return; }
        HandleScreenshot(request);
    }
    else
    {
        SendError(request, 404, "not_found", "endpoint not found");
    }
}

static void RegisterWebEndpoint(dmExtension::AppParams* params)
{
    g_Agent.m_WebServer = dmEngine::GetWebServer(params);
    if (!g_Agent.m_WebServer)
    {
        dmLogWarning("Defold Agent extension: debug web server is unavailable");
        return;
    }

    dmWebServer::HandlerParams handler_params;
    handler_params.m_Userdata = 0;
    handler_params.m_Handler = AgentHandler;

    dmWebServer::Result result = dmWebServer::AddHandler(g_Agent.m_WebServer, API_PREFIX, &handler_params);
    if (result == dmWebServer::RESULT_OK || result == dmWebServer::RESULT_HANDLER_ALREADY_REGISTRED)
    {
        g_Agent.m_WebHandlerRegistered = true;
        dmLogInfo("Defold Agent endpoint registered at `%s`", API_PREFIX);
    }
    else
    {
        dmLogWarning("Unable to register Defold Agent endpoint '%s' (%d)", API_PREFIX, result);
    }
}

static void UnregisterWebEndpoint()
{
    if (!g_Agent.m_WebServer || !g_Agent.m_WebHandlerRegistered)
    {
        g_Agent.m_WebServer = 0;
        g_Agent.m_WebHandlerRegistered = false;
        return;
    }
    dmWebServer::Result result = dmWebServer::RemoveHandler(g_Agent.m_WebServer, API_PREFIX);
    if (result != dmWebServer::RESULT_OK && result != dmWebServer::RESULT_HANDLER_NOT_REGISTRED)
    {
        dmLogWarning("Unable to remove Defold Agent endpoint '%s' (%d)", API_PREFIX, result);
    }
    g_Agent.m_WebServer = 0;
    g_Agent.m_WebHandlerRegistered = false;
}

static ExtensionResult PostRender(ExtensionParams* params)
{
    (void)params;
    ProcessPendingScreenshot();
    return EXTENSION_RESULT_OK;
}

static dmExtension::Result AppInitialize(dmExtension::AppParams* params)
{
    g_Agent = AgentContext();
    g_Agent.m_Register = dmEngine::GetGameObjectRegister(params);
    g_Agent.m_HidContext = dmEngine::GetHIDContext(params);
    g_Agent.m_LastTime = dmTime::GetTime();
    g_Agent.m_Initialized = true;
    RegisterWebEndpoint(params);
    dmExtension::RegisterCallback(dmExtension::CALLBACK_POST_RENDER, PostRender);
    return dmExtension::RESULT_OK;
}

static dmExtension::Result Initialize(dmExtension::Params* params)
{
    g_Agent.m_GraphicsContext = (dmGraphics::HContext)ExtensionParamsGetContextByName((ExtensionParams*)params, "graphics");
    dmLogInfo("Registered %s extension", LIB_NAME);
    return dmExtension::RESULT_OK;
}

static dmExtension::Result AppFinalize(dmExtension::AppParams* params)
{
    (void)params;
    UnregisterWebEndpoint();
    return dmExtension::RESULT_OK;
}

static dmExtension::Result Finalize(dmExtension::Params* params)
{
    (void)params;
    return dmExtension::RESULT_OK;
}

static dmExtension::Result OnUpdate(dmExtension::Params* params)
{
    (void)params;
    if (!g_Agent.m_Initialized)
    {
        return dmExtension::RESULT_OK;
    }

    uint64_t time = dmTime::GetTime();
    float dt = (float)((time - g_Agent.m_LastTime) / 1000000.0);
    g_Agent.m_LastTime = time;

    UpdateSnapshot();
    UpdateInput(dt);

    return dmExtension::RESULT_OK;
}

}

DM_DECLARE_EXTENSION(MODULE_NAME, LIB_NAME, dmAgent::AppInitialize, dmAgent::AppFinalize, dmAgent::Initialize, dmAgent::OnUpdate, 0, dmAgent::Finalize)

#else

static dmExtension::Result InitializeAgent(dmExtension::Params* params)
{
    (void)params;
    dmLogInfo("Registered %s extension (null)", LIB_NAME);
    return dmExtension::RESULT_OK;
}

static dmExtension::Result FinalizeAgent(dmExtension::Params* params)
{
    (void)params;
    return dmExtension::RESULT_OK;
}

DM_DECLARE_EXTENSION(MODULE_NAME, LIB_NAME, 0, 0, InitializeAgent, 0, 0, FinalizeAgent)

#endif

#undef LIB_NAME
