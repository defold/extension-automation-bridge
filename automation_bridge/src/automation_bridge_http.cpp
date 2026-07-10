#include "automation_bridge_private.h"
#include "automation_bridge_defold_private_api.h"

#if defined(DM_DEBUG)

#include <ctype.h>
#include <limits.h>
#include <string.h>

namespace dmAutomationBridge
{
    static const uint32_t MAX_SCREEN_DIMENSION = (uint32_t)INT_MAX;

    struct RequestContext
    {
        dmWebServer::Request*    m_Request;
        char*                    m_Path;
        char*                    m_Route;
        Array<QueryParam>      m_Query;
    };

    typedef void (*RouteHandler)(RequestContext* ctx);

    struct RouteDefinition
    {
        const char*  m_Route;
        const char*  m_Method;
        RouteHandler m_Handler;
    };
    static void SendResponse(dmWebServer::Request* request, int status_code, const char* content_type, const StringBuffer* response, const char* allow = 0)
    {
        const char* data = response->m_Data ? response->m_Data : "";
        dmWebServer::SetStatusCode(request, status_code);
        dmWebServer::SendAttribute(request, "Content-Type", content_type);
        dmWebServer::SendAttribute(request, "Cache-Control", "no-store");
        if (!IsEmpty(allow))
        {
            dmWebServer::SendAttribute(request, "Allow", allow);
        }
        dmWebServer::Send(request, data, response->m_Size);
    }

    static void RequestSendJson(RequestContext* ctx, int status_code, StringBuffer* response)
    {
        SendResponse(ctx->m_Request, status_code, "application/json", response);
        StringBufferFree(response);
    }

    static void RequestSendError(RequestContext* ctx, int status_code, const char* code, const char* message)
    {
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":false,\"error\":{\"code\":");
        AppendJsonString(&response, code);
        StringBufferAppend(&response, ",\"message\":");
        AppendJsonString(&response, message);
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, status_code, &response);
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

    static char* ReceiveRequestBody(dmWebServer::Request* request, uint32_t max_size)
    {
        if (!request || request->m_ContentLength == 0 || request->m_ContentLength > max_size)
        {
            return 0;
        }
        char* body = (char*)malloc(request->m_ContentLength + 1);
        if (!body)
        {
            return 0;
        }
        uint32_t offset = 0;
        while (offset < request->m_ContentLength)
        {
            uint32_t received = 0;
            dmWebServer::Result result = dmWebServer::Receive(request, body + offset, request->m_ContentLength - offset, &received);
            if (result != dmWebServer::RESULT_OK || received == 0)
            {
                free(body);
                return 0;
            }
            offset += received;
        }
        body[offset] = 0;
        return body;
    }

    static void SkipJsonWhitespace(const char** cursor)
    {
        while (**cursor && isspace((unsigned char)**cursor)) ++*cursor;
    }

    static char* ParseJsonString(const char** cursor, uint32_t max_size)
    {
        if (**cursor != '"') return 0;
        ++*cursor;
        StringBuffer value;
        StringBufferInit(&value);
        while (**cursor && **cursor != '"')
        {
            char c = *(*cursor)++;
            if (c == '\\')
            {
                char escaped = *(*cursor)++;
                if (!escaped || escaped == 'u')
                {
                    StringBufferFree(&value);
                    return 0;
                }
                if (escaped == 'b') c = '\b';
                else if (escaped == 'f') c = '\f';
                else if (escaped == 'n') c = '\n';
                else if (escaped == 'r') c = '\r';
                else if (escaped == 't') c = '\t';
                else if (escaped == '"' || escaped == '\\' || escaped == '/') c = escaped;
                else
                {
                    StringBufferFree(&value);
                    return 0;
                }
            }
            if ((unsigned char)c < 0x20 || value.m_Size >= max_size)
            {
                StringBufferFree(&value);
                return 0;
            }
            StringBufferAppendChar(&value, c);
        }
        if (**cursor != '"')
        {
            StringBufferFree(&value);
            return 0;
        }
        ++*cursor;
        return StringBufferDetach(&value);
    }

    static bool AddJsonQueryParam(Array<QueryParam>* query, char* key, char* value)
    {
        QueryParam param;
        param.m_Key = key;
        param.m_Value = value;
        return ArrayPush(query, &param);
    }

    static bool ParseFlatJsonObject(const char* body, Array<QueryParam>* query)
    {
        const char* cursor = body;
        SkipJsonWhitespace(&cursor);
        if (*cursor++ != '{') return false;
        SkipJsonWhitespace(&cursor);
        if (*cursor == '}')
        {
            ++cursor;
            SkipJsonWhitespace(&cursor);
            return *cursor == 0;
        }
        while (*cursor)
        {
            SkipJsonWhitespace(&cursor);
            char* key = ParseJsonString(&cursor, 64);
            if (!key) return false;
            SkipJsonWhitespace(&cursor);
            if (*cursor++ != ':')
            {
                FreeString(&key);
                return false;
            }
            SkipJsonWhitespace(&cursor);
            char* value = 0;
            if (*cursor == '"')
            {
                value = ParseJsonString(&cursor, 8192);
            }
            else
            {
                const char* start = cursor;
                while (*cursor && *cursor != ',' && *cursor != '}' && !isspace((unsigned char)*cursor)) ++cursor;
                uint32_t length = (uint32_t)(cursor - start);
                if (length == 0 || length > 64)
                {
                    FreeString(&key);
                    return false;
                }
                value = DuplicateStringN(start, length);
                if (value && !StringsEqual(value, "true") && !StringsEqual(value, "false"))
                {
                    char* end = 0;
                    strtod(value, &end);
                    if (!end || *end != 0)
                    {
                        FreeString(&value);
                    }
                }
            }
            if (!value || !AddJsonQueryParam(query, key, value))
            {
                FreeString(&key);
                FreeString(&value);
                return false;
            }
            SkipJsonWhitespace(&cursor);
            if (*cursor == ',')
            {
                ++cursor;
                continue;
            }
            if (*cursor == '}')
            {
                ++cursor;
                SkipJsonWhitespace(&cursor);
                return *cursor == 0;
            }
            return false;
        }
        return false;
    }

    static void InitRequestContext(RequestContext* ctx, dmWebServer::Request* request)
    {
        memset(ctx, 0, sizeof(*ctx));
        ctx->m_Request = request;
        ParseResource(request ? request->m_Resource : 0, &ctx->m_Path, &ctx->m_Query);
        if (StartsWith(ctx->m_Path, API_PREFIX))
        {
            const char* route = ctx->m_Path + strlen(API_PREFIX);
            SetString(&ctx->m_Route, route[0] ? route : "/");
        }
    }

    static void FreeRequestContext(RequestContext* ctx)
    {
        FreeString(&ctx->m_Path);
        FreeString(&ctx->m_Route);
        FreeQueryParams(&ctx->m_Query);
    }

    static bool RequestHasApiPrefix(const RequestContext* ctx)
    {
        return StartsWith(ctx->m_Path, API_PREFIX);
    }

    static bool RequestIsMethod(const RequestContext* ctx, const char* method)
    {
        return ctx->m_Request->m_Method && strcmp(ctx->m_Request->m_Method, method) == 0;
    }

    static bool RequestGetFloatParam(const RequestContext* ctx, const char* key, float* value)
    {
        return GetFloatParam(&ctx->m_Query, key, value);
    }

    static bool RequestGetBoolParam(const RequestContext* ctx, const char* key, bool* value)
    {
        return GetBoolParam(&ctx->m_Query, key, value);
    }

    static bool RequestGetUIntParam(const RequestContext* ctx, const char* key, uint32_t* value, uint32_t max_value = 0xffffffffU)
    {
        const char* text = GetParam(&ctx->m_Query, key);
        if (IsEmpty(text))
        {
            return false;
        }
        if (!isdigit((unsigned char)text[0]))
        {
            return false;
        }

        char* end = 0;
        unsigned long parsed = strtoul(text, &end, 10);
        if (!end || *end != 0 || parsed == 0 || parsed > (unsigned long)max_value)
        {
            return false;
        }

        *value = (uint32_t)parsed;
        return true;
    }

    static bool RequestGetUInt64Param(const RequestContext* ctx, const char* key, uint64_t* value, bool allow_zero = false)
    {
        const char* text = GetParam(&ctx->m_Query, key);
        if (IsEmpty(text) || !isdigit((unsigned char)text[0]))
        {
            return false;
        }
        char* end = 0;
        unsigned long long parsed = strtoull(text, &end, 10);
        if (!end || *end != 0 || (!allow_zero && parsed == 0))
        {
            return false;
        }
        *value = (uint64_t)parsed;
        return true;
    }

    static const char* RequestGetParam(const RequestContext* ctx, const char* key)
    {
        return GetParam(&ctx->m_Query, key);
    }

    static bool IncludeHasToken(const char* include, const char* token)
    {
        if (!include)
        {
            return false;
        }

        uint32_t token_len = (uint32_t)strlen(token);
        const char* start = include;
        while (*start)
        {
            const char* end = strchr(start, ',');
            if (!end)
            {
                end = start + strlen(start);
            }

            const char* first = start;
            while (first < end && isspace((unsigned char)*first))
            {
                ++first;
            }

            const char* last = end;
            while (last > first && isspace((unsigned char)*(last - 1)))
            {
                --last;
            }

            if ((uint32_t)(last - first) == token_len && strncmp(first, token, token_len) == 0)
            {
                return true;
            }

            if (*end == 0)
            {
                break;
            }
            start = end + 1;
        }
        return false;
    }

    static IncludeOptions ParseInclude(const RequestContext* ctx, bool default_properties, bool default_children)
    {
        IncludeOptions options;
        options.m_Bounds = true;
        options.m_Properties = default_properties;
        options.m_Children = default_children;

        const char* include = RequestGetParam(ctx, "include");
        if (IsEmpty(include))
        {
            return options;
        }

        bool all = IncludeHasToken(include, "all");
        options.m_Bounds = all || IncludeHasToken(include, "bounds");
        options.m_Properties = all || IncludeHasToken(include, "properties");
        options.m_Children = all || IncludeHasToken(include, "children");
        return options;
    }

    static void RefreshSnapshotForRequest()
    {
        if (g_AutomationBridge.m_Snapshot.m_Sequence == 0 || g_AutomationBridge.m_SnapshotFrame != g_AutomationBridge.m_Frame)
        {
            UpdateSnapshot();
            g_AutomationBridge.m_SnapshotFrame = g_AutomationBridge.m_Frame;
        }
    }

    static bool ValidateExpectedScene(RequestContext* ctx)
    {
        const char* expected_text = RequestGetParam(ctx, "expected_scene_sequence");
        if (IsEmpty(expected_text))
        {
            return true;
        }
        uint64_t expected = 0;
        if (!RequestGetUInt64Param(ctx, "expected_scene_sequence", &expected))
        {
            RequestSendError(ctx, 400, "bad_request", "expected_scene_sequence must be a positive integer");
            return false;
        }
        if (expected != g_AutomationBridge.m_Snapshot.m_Sequence)
        {
            RequestSendError(ctx, 409, "stale_scene", "scene sequence changed before input target resolution; refresh the scene and target");
            return false;
        }
        return true;
    }

    static void HandleHealth(RequestContext* ctx)
    {
        RefreshSnapshotForRequest();

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"version\":");
        AppendJsonString(&response, API_VERSION);
        StringBufferAppend(&response, ",\"debug\":true,\"platform\":");
#if defined(DM_PLATFORM_OSX)
        AppendJsonString(&response, "macos");
#elif defined(DM_PLATFORM_WINDOWS)
        AppendJsonString(&response, "windows");
#elif defined(DM_PLATFORM_LINUX)
        AppendJsonString(&response, "linux");
#elif defined(DM_PLATFORM_ANDROID)
        AppendJsonString(&response, "android");
#elif defined(DM_PLATFORM_IOS)
        AppendJsonString(&response, "ios");
#else
        AppendJsonString(&response, "unknown");
#endif
        StringBufferAppend(&response, ",\"capabilities\":[\"scene\",\"nodes\",\"node\"");
        if (DefoldPrivateApiCanSetWindowSize())
        {
            StringBufferAppend(&response, ",\"screen.resize\"");
        }
        StringBufferAppend(&response, ",\"input.click\",\"input.drag\",\"input.drag_path\",\"input.pointer\",\"input.key\",\"input.receipts\",\"input.queue\",\"input.controller\",\"input.device.mouse\"");
        if (IsInputDeviceSupported(INPUT_DEVICE_TOUCH))
        {
            StringBufferAppend(&response, ",\"input.device.touch\"");
        }
        if (IsScreenshotSupported())
        {
            StringBufferAppend(&response, ",\"screenshot\"");
        }
        StringBufferAppend(&response, "],\"screen\":");
        AppendScreenJson(&response, &g_AutomationBridge.m_Snapshot);
        StringBufferAppend(&response, ",\"scene_sequence\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Snapshot.m_Sequence);
        StringBufferAppend(&response, ",\"engine_frame\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Frame);
        StringBufferAppend(&response, ",\"engine_instance_id\":");
        AppendJsonString(&response, g_AutomationBridge.m_EngineInstanceId);
        StringBufferAppend(&response, ",\"input\":{\"default_device\":");
        AppendJsonString(&response, InputDeviceName(g_AutomationBridge.m_DefaultInputDevice));
        StringBufferAppend(&response, ",\"default_visualize\":");
        StringBufferAppend(&response, g_AutomationBridge.m_DefaultInputVisualize ? "true" : "false");
        StringBufferAppend(&response, ",\"devices\":{\"auto\":true,\"mouse\":true,\"touch\":");
        StringBufferAppend(&response, IsInputDeviceSupported(INPUT_DEVICE_TOUCH) ? "true" : "false");
        StringBufferAppend(&response, "}}");
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleScreen(RequestContext* ctx)
    {
        RefreshSnapshotForRequest();

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":");
        AppendScreenJson(&response, &g_AutomationBridge.m_Snapshot);
        StringBufferAppend(&response, "}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleScreenPut(RequestContext* ctx)
    {
        uint32_t width = 0;
        uint32_t height = 0;
        if (!RequestGetUIntParam(ctx, "width", &width, MAX_SCREEN_DIMENSION) ||
            !RequestGetUIntParam(ctx, "height", &height, MAX_SCREEN_DIMENSION))
        {
            RequestSendError(ctx, 400, "bad_request", "provide positive integer width and height no greater than 2147483647");
            return;
        }

        if (!DefoldPrivateApiSetWindowSize(width, height))
        {
            RequestSendError(ctx, 500, "screen_resize_failed", "failed to set window size");
            return;
        }

        UpdateSnapshot();
        g_AutomationBridge.m_SnapshotFrame = g_AutomationBridge.m_Frame;

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":");
        AppendScreenJson(&response, &g_AutomationBridge.m_Snapshot);
        StringBufferAppend(&response, "}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleScene(RequestContext* ctx)
    {
        RefreshSnapshotForRequest();

        IncludeOptions include = ParseInclude(ctx, false, true);
        bool visible_only = false;
        RequestGetBoolParam(ctx, "visible", &visible_only);

        const Snapshot* snapshot = &g_AutomationBridge.m_Snapshot;
        if (snapshot->m_Root < 0 || (uint32_t)snapshot->m_Root >= snapshot->m_Nodes.m_Count)
        {
            RequestSendError(ctx, 404, "scene_unavailable", "scene graph is not available");
            return;
        }

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"count\":");
        AppendNumber(&response, (double)snapshot->m_Nodes.m_Count);
        StringBufferAppend(&response, ",\"visible_filter\":");
        StringBufferAppend(&response, visible_only ? "true" : "false");
        StringBufferAppend(&response, ",\"screen\":");
        AppendScreenJson(&response, snapshot);
        StringBufferAppend(&response, ",\"root\":");
        AppendNodeJson(&response, snapshot, &snapshot->m_Nodes.m_Data[snapshot->m_Root], &include, true, visible_only);
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static bool NodeMatchesFilters(const Node* node, const RequestContext* ctx)
    {
        const char* value = RequestGetParam(ctx, "id");
        if (value && !StringsEqual(node->m_Id, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "type");
        if (value && !ContainsCaseInsensitive(node->m_Type, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "name");
        if (value && !ContainsCaseInsensitive(node->m_Name, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "text");
        if (value && !ContainsCaseInsensitive(node->m_Text, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "url");
        if (value && !ContainsCaseInsensitive(node->m_Url, value))
        {
            return false;
        }
        bool visible = false;
        if (RequestGetBoolParam(ctx, "visible", &visible) && node->m_Visible != visible)
        {
            return false;
        }
        return true;
    }

    static void HandleNodes(RequestContext* ctx)
    {
        RefreshSnapshotForRequest();

        IncludeOptions include = ParseInclude(ctx, false, false);
        float limit_f = 50.0f;
        RequestGetFloatParam(ctx, "limit", &limit_f);
        uint32_t limit = (uint32_t)ClampFloat(limit_f, 0.0f, 500.0f);

        const Snapshot* snapshot = &g_AutomationBridge.m_Snapshot;
        uint32_t matched = 0;
        uint32_t emitted = 0;

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"nodes\":[");
        for (uint32_t i = 0; i < snapshot->m_Nodes.m_Count; ++i)
        {
            const Node* node = &snapshot->m_Nodes.m_Data[i];
            if (!NodeMatchesFilters(node, ctx))
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
                StringBufferAppendChar(&response, ',');
            }
            AppendNodeJson(&response, snapshot, node, &include, false, false);
            ++emitted;
        }
        StringBufferAppend(&response, "],\"count\":");
        AppendNumber(&response, (double)emitted);
        StringBufferAppend(&response, ",\"matched\":");
        AppendNumber(&response, (double)matched);
        StringBufferAppend(&response, ",\"total\":");
        AppendNumber(&response, (double)snapshot->m_Nodes.m_Count);
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleNode(RequestContext* ctx)
    {
        RefreshSnapshotForRequest();

        const char* id = RequestGetParam(ctx, "id");
        if (IsEmpty(id))
        {
            RequestSendError(ctx, 400, "bad_request", "missing node id");
            return;
        }

        const Node* node = FindNodeById(id);
        if (!node)
        {
            RequestSendError(ctx, 404, "not_found", "node id was not found");
            return;
        }

        IncludeOptions include = ParseInclude(ctx, true, false);
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"node\":");
        AppendNodeJson(&response, &g_AutomationBridge.m_Snapshot, node, &include, include.m_Children, false);
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static bool GetInputIdentity(RequestContext* ctx, const char** client_id, const char** session_id,
                                 const char** request_id, float* lease)
    {
        *client_id = RequestGetParam(ctx, "client_id");
        *session_id = RequestGetParam(ctx, "session_id");
        *request_id = RequestGetParam(ctx, "request_id");
        *lease = 5.0f;
        const char* lease_text = RequestGetParam(ctx, "lease");
        if (!IsEmpty(lease_text) && (!RequestGetFloatParam(ctx, "lease", lease) || !IsFiniteFloat(*lease) || *lease <= 0.0f || *lease > MAX_INPUT_DURATION))
        {
            RequestSendError(ctx, 400, "bad_request", "lease must be finite and between 0 and 60 seconds");
            return false;
        }
        return true;
    }

    static bool AcquireControllerForRequest(RequestContext* ctx, const char* client_id, const char* session_id, float lease)
    {
        const char* error = 0;
        if (!AcquireInputController(client_id, session_id, lease, &error))
        {
            RequestSendError(ctx, 409, "input_controller_busy", error);
            return false;
        }
        return true;
    }

    static bool GetInputOptions(RequestContext* ctx, InputDevice* device, uint32_t* pointer_id, bool* visualize)
    {
        *device = g_AutomationBridge.m_DefaultInputDevice;
        const char* device_text = RequestGetParam(ctx, "device");
        if (!IsEmpty(device_text) && !ParseInputDevice(device_text, device))
        {
            RequestSendError(ctx, 400, "bad_request", "device must be auto, mouse, or touch");
            return false;
        }
        if (!IsInputDeviceSupported(ResolveInputDevice(*device)))
        {
            RequestSendError(ctx, 422, "input_device_unsupported", "requested input device is not supported on this platform");
            return false;
        }
        *pointer_id = 0;
        uint64_t pointer_value = 0;
        const char* pointer_text = RequestGetParam(ctx, "pointer_id");
        if (!IsEmpty(pointer_text))
        {
            if (!RequestGetUInt64Param(ctx, "pointer_id", &pointer_value, true) || pointer_value > 0xffffffffU)
            {
                RequestSendError(ctx, 400, "bad_request", "pointer_id must be an unsigned 32-bit integer");
                return false;
            }
            *pointer_id = (uint32_t)pointer_value;
        }
        *visualize = g_AutomationBridge.m_DefaultInputVisualize;
        RequestGetBoolParam(ctx, "visualize", visualize);
        return true;
    }

    static void SendReceiptResponse(RequestContext* ctx, const InputReceipt* receipt, uint32_t queue_position, int status_code = 202)
    {
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":");
        AppendInputReceiptJson(&response, receipt, queue_position);
        StringBufferAppend(&response, "}\n");
        RequestSendJson(ctx, status_code, &response);
    }

    static bool ValidateDuration(RequestContext* ctx, float value, const char* name)
    {
        if (IsFiniteFloat(value) && value >= 0.0f && value <= MAX_INPUT_DURATION)
        {
            return true;
        }
        StringBuffer message;
        StringBufferInit(&message);
        StringBufferAppend(&message, name);
        StringBufferAppend(&message, " must be finite and between 0 and 60 seconds");
        char* text = StringBufferDetach(&message);
        RequestSendError(ctx, 400, "bad_request", text);
        free(text);
        return false;
    }

    static bool ParsePointList(const char* value, Array<InputPoint>* points)
    {
        ArrayInit(points);
        if (IsEmpty(value) || strlen(value) > 8192) return false;
        const char* cursor = value;
        while (*cursor)
        {
            char* end = 0;
            double x = strtod(cursor, &end);
            if (!end || end == cursor || *end != ',') return false;
            cursor = end + 1;
            double y = strtod(cursor, &end);
            if (!end || end == cursor || (*end != ';' && *end != 0) || !IsFiniteDouble(x) || !IsFiniteDouble(y)) return false;
            InputPoint point;
            point.m_X = (float)x;
            point.m_Y = (float)y;
            point.m_Duration = 0.0f;
            point.m_Easing = INPUT_EASING_LINEAR;
            if (!ArrayPush(points, &point) || points->m_Count > MAX_INPUT_PATH_POINTS) return false;
            cursor = *end == ';' ? end + 1 : end;
            if (*cursor == 0) break;
        }
        return points->m_Count > 0;
    }

    static bool ParseDurationList(const char* value, float* durations, uint32_t expected)
    {
        if (expected == 0) return IsEmpty(value);
        if (IsEmpty(value)) return false;
        const char* cursor = value;
        for (uint32_t i = 0; i < expected; ++i)
        {
            char* end = 0;
            double duration = strtod(cursor, &end);
            if (!end || end == cursor || !IsFiniteDouble(duration) || duration < 0.0 || duration > MAX_INPUT_DURATION) return false;
            durations[i] = (float)duration;
            if (i + 1 < expected)
            {
                if (*end != ',') return false;
                cursor = end + 1;
            }
            else if (*end != 0)
            {
                return false;
            }
        }
        return true;
    }

    static bool ParseEasingList(const char* value, InputEasing* easing, uint32_t expected)
    {
        if (IsEmpty(value))
        {
            for (uint32_t i = 0; i < expected; ++i) easing[i] = INPUT_EASING_LINEAR;
            return true;
        }
        const char* cursor = value;
        for (uint32_t i = 0; i < expected; ++i)
        {
            const char* end = strchr(cursor, ',');
            uint32_t length = end ? (uint32_t)(end - cursor) : (uint32_t)strlen(cursor);
            if (length == 0 || length >= 32) return false;
            char token[32];
            memcpy(token, cursor, length);
            token[length] = 0;
            if (!ParseInputEasing(token, &easing[i])) return false;
            if (i + 1 < expected)
            {
                if (!end) return false;
                cursor = end + 1;
            }
            else if (end)
            {
                return false;
            }
        }
        return true;
    }

    static bool QueuePointerPath(RequestContext* ctx, Array<InputPoint>* points, InputPathMode mode,
                                 float hold_before, float hold_after, const char* kind, bool pointer_open, float pointer_lease)
    {
        InputDevice device;
        uint32_t pointer_id = 0;
        bool visualize = true;
        if (!GetInputOptions(ctx, &device, &pointer_id, &visualize)) return false;
        const char* client_id = 0;
        const char* session_id = 0;
        const char* request_id = 0;
        float controller_lease = 5.0f;
        if (!GetInputIdentity(ctx, &client_id, &session_id, &request_id, &controller_lease) ||
            !AcquireControllerForRequest(ctx, client_id, session_id, controller_lease)) return false;
        InputReceipt* receipt = 0;
        if (!AddMouseInput(points, mode, hold_before, hold_after, device, pointer_id, visualize, kind,
                           client_id, session_id, request_id, g_AutomationBridge.m_Snapshot.m_Sequence,
                           pointer_lease, pointer_open, &receipt))
        {
            RequestSendError(ctx, 429, "input_queue_full", "input could not be queued; check device support and queue capacity");
            return false;
        }
        SendReceiptResponse(ctx, receipt, g_AutomationBridge.m_InputEvents.m_Count);
        return true;
    }

    static void HandleClick(RequestContext* ctx)
    {
        RefreshSnapshotForRequest();
        if (!ValidateExpectedScene(ctx)) return;
        float x = 0.0f;
        float y = 0.0f;
        const char* id = RequestGetParam(ctx, "id");
        if (!IsEmpty(id))
        {
            const char* error = 0;
            if (!GetNodeCenter(id, &x, &y, &error))
            {
                RequestSendError(ctx, 404, "not_found", error);
                return;
            }
        }
        else if (!RequestGetFloatParam(ctx, "x", &x) || !RequestGetFloatParam(ctx, "y", &y) || !IsFiniteFloat(x) || !IsFiniteFloat(y))
        {
            RequestSendError(ctx, 400, "bad_request", "provide either id or finite x/y");
            return;
        }
        Array<InputPoint> points;
        ArrayInit(&points);
        InputPoint point = {x, y, 0.0f, INPUT_EASING_LINEAR};
        ArrayPush(&points, &point);
        QueuePointerPath(ctx, &points, INPUT_PATH_SAMPLED, 0.0f, 0.0f, "click", false, 0.0f);
        ArrayFree(&points);
    }

    static void HandleDrag(RequestContext* ctx)
    {
        RefreshSnapshotForRequest();
        if (!ValidateExpectedScene(ctx)) return;
        float x1 = 0.0f, y1 = 0.0f, x2 = 0.0f, y2 = 0.0f;
        float duration = 0.35f;
        RequestGetFloatParam(ctx, "duration", &duration);
        if (!ValidateDuration(ctx, duration, "duration")) return;
        const char* from_id = RequestGetParam(ctx, "from_id");
        const char* to_id = RequestGetParam(ctx, "to_id");
        if (!IsEmpty(from_id) && !IsEmpty(to_id))
        {
            const char* error = 0;
            if (!GetNodeCenter(from_id, &x1, &y1, &error) || !GetNodeCenter(to_id, &x2, &y2, &error))
            {
                RequestSendError(ctx, 404, "not_found", error);
                return;
            }
        }
        else if (!RequestGetFloatParam(ctx, "x1", &x1) || !RequestGetFloatParam(ctx, "y1", &y1) ||
                 !RequestGetFloatParam(ctx, "x2", &x2) || !RequestGetFloatParam(ctx, "y2", &y2) ||
                 !IsFiniteFloat(x1) || !IsFiniteFloat(y1) || !IsFiniteFloat(x2) || !IsFiniteFloat(y2))
        {
            RequestSendError(ctx, 400, "bad_request", "provide either from_id/to_id or finite x1/y1/x2/y2");
            return;
        }
        InputEasing easing = INPUT_EASING_LINEAR;
        if (!ParseInputEasing(RequestGetParam(ctx, "easing"), &easing))
        {
            RequestSendError(ctx, 400, "bad_request", "unsupported easing");
            return;
        }
        float hold_before = 0.0f, hold_after = 0.0f;
        RequestGetFloatParam(ctx, "hold_before", &hold_before);
        RequestGetFloatParam(ctx, "hold_after", &hold_after);
        if (!ValidateDuration(ctx, hold_before, "hold_before")) return;
        if (!ValidateDuration(ctx, hold_after, "hold_after")) return;
        if (duration + hold_before + hold_after > MAX_INPUT_DURATION)
        {
            RequestSendError(ctx, 400, "bad_request", "total gesture duration exceeds 60 seconds");
            return;
        }
        Array<InputPoint> points;
        ArrayInit(&points);
        InputPoint start = {x1, y1, 0.0f, INPUT_EASING_LINEAR};
        InputPoint end = {x2, y2, duration, easing};
        ArrayPush(&points, &start);
        ArrayPush(&points, &end);
        QueuePointerPath(ctx, &points, INPUT_PATH_SAMPLED, hold_before, hold_after, "drag", false, 0.0f);
        ArrayFree(&points);
    }

    static void HandleDragPath(RequestContext* ctx)
    {
        RefreshSnapshotForRequest();
        if (!ValidateExpectedScene(ctx)) return;
        Array<InputPoint> points;
        if (!ParsePointList(RequestGetParam(ctx, "points"), &points))
        {
            ArrayFree(&points);
            RequestSendError(ctx, 400, "bad_request", "points must contain 2-128 finite x,y pairs separated by semicolons");
            return;
        }
        InputPathMode mode = INPUT_PATH_SAMPLED;
        const char* mode_text = RequestGetParam(ctx, "path");
        if (!IsEmpty(mode_text) && StringsEqual(mode_text, "quadratic")) mode = INPUT_PATH_QUADRATIC;
        else if (!IsEmpty(mode_text) && StringsEqual(mode_text, "cubic")) mode = INPUT_PATH_CUBIC;
        else if (!IsEmpty(mode_text) && !StringsEqual(mode_text, "sampled") && !StringsEqual(mode_text, "linear"))
        {
            ArrayFree(&points);
            RequestSendError(ctx, 400, "bad_request", "path must be sampled, linear, quadratic, or cubic");
            return;
        }
        uint32_t segment_count = mode == INPUT_PATH_SAMPLED ? points.m_Count - 1 : 1;
        if (points.m_Count < 2 || (mode == INPUT_PATH_QUADRATIC && points.m_Count != 3) || (mode == INPUT_PATH_CUBIC && points.m_Count != 4))
        {
            ArrayFree(&points);
            RequestSendError(ctx, 400, "bad_request", "sampled paths need at least 2 points; quadratic needs 3; cubic needs 4");
            return;
        }
        float durations[MAX_INPUT_PATH_POINTS];
        InputEasing easing[MAX_INPUT_PATH_POINTS];
        if (!ParseDurationList(RequestGetParam(ctx, "durations"), durations, segment_count) ||
            !ParseEasingList(RequestGetParam(ctx, "easing"), easing, segment_count))
        {
            ArrayFree(&points);
            RequestSendError(ctx, 400, "bad_request", "durations and easing must contain exactly one value per path segment");
            return;
        }
        float total = 0.0f;
        for (uint32_t i = 0; i < segment_count; ++i)
        {
            total += durations[i];
            uint32_t point_index = mode == INPUT_PATH_SAMPLED ? i + 1 : points.m_Count - 1;
            points.m_Data[point_index].m_Duration = durations[i];
            points.m_Data[point_index].m_Easing = easing[i];
        }
        float hold_before = 0.0f, hold_after = 0.0f;
        RequestGetFloatParam(ctx, "hold_before", &hold_before);
        RequestGetFloatParam(ctx, "hold_after", &hold_after);
        if (!ValidateDuration(ctx, hold_before, "hold_before") || !ValidateDuration(ctx, hold_after, "hold_after"))
        {
            ArrayFree(&points);
            return;
        }
        if (total + hold_before + hold_after > MAX_INPUT_DURATION)
        {
            RequestSendError(ctx, 400, "bad_request", "total gesture duration exceeds 60 seconds");
            ArrayFree(&points);
            return;
        }
        QueuePointerPath(ctx, &points, mode, hold_before, hold_after, "drag_path", false, 0.0f);
        ArrayFree(&points);
    }

    static void HandleKey(RequestContext* ctx)
    {
        RefreshSnapshotForRequest();
        if (!ValidateExpectedScene(ctx)) return;
        const char* value = RequestGetParam(ctx, "keys");
        if (!value) value = RequestGetParam(ctx, "text");
        if (IsEmpty(value) || strlen(value) > MAX_KEY_INPUT_BYTES)
        {
            RequestSendError(ctx, IsEmpty(value) ? 400 : 413, IsEmpty(value) ? "bad_request" : "input_too_large", "provide text/keys no larger than 4096 bytes");
            return;
        }
        const char* client_id = 0;
        const char* session_id = 0;
        const char* request_id = 0;
        float lease = 5.0f;
        if (!GetInputIdentity(ctx, &client_id, &session_id, &request_id, &lease) || !AcquireControllerForRequest(ctx, client_id, session_id, lease)) return;
        InputReceipt* receipt = 0;
        if (!AddKeyInput(value, client_id, session_id, request_id, g_AutomationBridge.m_Snapshot.m_Sequence, &receipt))
        {
            RequestSendError(ctx, 429, "input_queue_full", "too many input events are already queued");
            return;
        }
        SendReceiptResponse(ctx, receipt, g_AutomationBridge.m_InputEvents.m_Count);
    }

    static bool GetInputId(RequestContext* ctx, uint64_t* input_id)
    {
        if (!RequestGetUInt64Param(ctx, "input_id", input_id))
        {
            RequestSendError(ctx, 400, "bad_request", "input_id must be a positive integer");
            return false;
        }
        return true;
    }

    static uint32_t QueuePosition(uint64_t input_id)
    {
        for (uint32_t i = 0; i < g_AutomationBridge.m_InputEvents.m_Count; ++i)
            if (g_AutomationBridge.m_InputEvents.m_Data[i].m_Receipt.m_Id == input_id) return i + 1;
        return 0;
    }

    static void HandleInputStatus(RequestContext* ctx)
    {
        uint64_t input_id = 0;
        if (!GetInputId(ctx, &input_id)) return;
        const InputReceipt* receipt = FindInputReceipt(input_id);
        if (!receipt)
        {
            RequestSendError(ctx, 404, "input_not_found", "input receipt was not found or expired from bounded history");
            return;
        }
        SendReceiptResponse(ctx, receipt, QueuePosition(input_id), 200);
    }

    static void HandleInputPending(RequestContext* ctx)
    {
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"inputs\":[");
        for (uint32_t i = 0; i < g_AutomationBridge.m_InputEvents.m_Count; ++i)
        {
            if (i) StringBufferAppendChar(&response, ',');
            AppendInputReceiptJson(&response, &g_AutomationBridge.m_InputEvents.m_Data[i].m_Receipt, i + 1);
        }
        StringBufferAppend(&response, "],\"count\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_InputEvents.m_Count);
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static bool VerifyReceiptOwner(RequestContext* ctx, const InputReceipt* receipt, const char* client_id, const char* session_id)
    {
        if (receipt && StringsEqual(receipt->m_ClientId, IsEmpty(client_id) ? "anonymous" : client_id) &&
            StringsEqual(receipt->m_SessionId, IsEmpty(session_id) ? "default" : session_id)) return true;
        RequestSendError(ctx, 403, "input_not_owned", "input belongs to a different client/session");
        return false;
    }

    static void HandleInputCancel(RequestContext* ctx)
    {
        uint64_t input_id = 0;
        if (!GetInputId(ctx, &input_id)) return;
        const char* client_id = 0, *session_id = 0, *request_id = 0;
        float lease = 5.0f;
        if (!GetInputIdentity(ctx, &client_id, &session_id, &request_id, &lease) || !AcquireControllerForRequest(ctx, client_id, session_id, lease)) return;
        const InputReceipt* receipt = FindInputReceipt(input_id);
        if (!receipt)
        {
            RequestSendError(ctx, 404, "input_not_found", "input was not found");
            return;
        }
        if (!VerifyReceiptOwner(ctx, receipt, client_id, session_id)) return;
        bool release = true;
        RequestGetBoolParam(ctx, "release", &release);
        if (!CancelInput(input_id, release, "cancelled_by_client"))
        {
            RequestSendError(ctx, 409, "input_already_finished", "input is already in a terminal state");
            return;
        }
        SendReceiptResponse(ctx, FindInputReceipt(input_id), QueuePosition(input_id), 202);
    }

    static void HandleInputFlush(RequestContext* ctx)
    {
        const char* client_id = 0, *session_id = 0, *request_id = 0;
        float lease = 5.0f;
        if (!GetInputIdentity(ctx, &client_id, &session_id, &request_id, &lease) || !AcquireControllerForRequest(ctx, client_id, session_id, lease)) return;
        bool release = true;
        RequestGetBoolParam(ctx, "release", &release);
        uint32_t count = FlushInput(IsEmpty(client_id) ? "anonymous" : client_id, IsEmpty(session_id) ? "default" : session_id,
                                    release, "flushed_by_client");
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"cancel_requested\":");
        AppendNumber(&response, (double)count);
        StringBufferAppend(&response, ",\"release\":");
        StringBufferAppend(&response, release ? "true" : "false");
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 202, &response);
    }

    static void HandleInputConfigure(RequestContext* ctx)
    {
        const char* client_id = 0, *session_id = 0, *request_id = 0;
        float lease = 5.0f;
        if (!GetInputIdentity(ctx, &client_id, &session_id, &request_id, &lease) || !AcquireControllerForRequest(ctx, client_id, session_id, lease)) return;
        InputDevice device = g_AutomationBridge.m_DefaultInputDevice;
        const char* device_text = RequestGetParam(ctx, "device");
        if (!IsEmpty(device_text) && (!ParseInputDevice(device_text, &device) || !IsInputDeviceSupported(ResolveInputDevice(device))))
        {
            RequestSendError(ctx, 422, "input_device_unsupported", "device must be auto/mouse or a supported touch device");
            return;
        }
        bool visualize = g_AutomationBridge.m_DefaultInputVisualize;
        RequestGetBoolParam(ctx, "visualize", &visualize);
        g_AutomationBridge.m_DefaultInputDevice = device;
        g_AutomationBridge.m_DefaultInputVisualize = visualize;
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"device\":");
        AppendJsonString(&response, InputDeviceName(device));
        StringBufferAppend(&response, ",\"resolved_device\":");
        AppendJsonString(&response, InputDeviceName(ResolveInputDevice(device)));
        StringBufferAppend(&response, ",\"visualize\":");
        StringBufferAppend(&response, visualize ? "true" : "false");
        StringBufferAppend(&response, ",\"controller\":{\"client_id\":");
        AppendJsonString(&response, IsEmpty(client_id) ? "anonymous" : client_id);
        StringBufferAppend(&response, ",\"session_id\":");
        AppendJsonString(&response, IsEmpty(session_id) ? "default" : session_id);
        StringBufferAppend(&response, "},\"devices\":{\"auto\":true,\"mouse\":true,\"touch\":");
        StringBufferAppend(&response, IsInputDeviceSupported(INPUT_DEVICE_TOUCH) ? "true" : "false");
        StringBufferAppend(&response, "}}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandlePointerOpen(RequestContext* ctx)
    {
        RefreshSnapshotForRequest();
        if (!ValidateExpectedScene(ctx)) return;
        float x = 0.0f, y = 0.0f;
        if (!RequestGetFloatParam(ctx, "x", &x) || !RequestGetFloatParam(ctx, "y", &y) || !IsFiniteFloat(x) || !IsFiniteFloat(y))
        {
            RequestSendError(ctx, 400, "bad_request", "pointer open requires finite x/y");
            return;
        }
        float pointer_lease = 2.0f;
        RequestGetFloatParam(ctx, "pointer_lease", &pointer_lease);
        if (!ValidateDuration(ctx, pointer_lease, "pointer_lease")) return;
        if (pointer_lease <= 0.0f)
        {
            RequestSendError(ctx, 400, "bad_request", "pointer_lease must be greater than zero");
            return;
        }
        Array<InputPoint> points;
        ArrayInit(&points);
        InputPoint point = {x, y, 0.0f, INPUT_EASING_LINEAR};
        ArrayPush(&points, &point);
        QueuePointerPath(ctx, &points, INPUT_PATH_SAMPLED, 0.0f, 0.0f, "pointer", true, pointer_lease);
        ArrayFree(&points);
    }

    static bool PreparePointerCommand(RequestContext* ctx, uint64_t* input_id, float* pointer_lease,
                                      const char** client_id, const char** session_id)
    {
        if (!GetInputId(ctx, input_id)) return false;
        const char* request_id = 0;
        float controller_lease = 5.0f;
        if (!GetInputIdentity(ctx, client_id, session_id, &request_id, &controller_lease) ||
            !AcquireControllerForRequest(ctx, *client_id, *session_id, controller_lease)) return false;
        const InputReceipt* receipt = FindInputReceipt(*input_id);
        if (!receipt)
        {
            RequestSendError(ctx, 404, "input_not_found", "pointer session was not found");
            return false;
        }
        if (!VerifyReceiptOwner(ctx, receipt, *client_id, *session_id)) return false;
        *pointer_lease = 2.0f;
        RequestGetFloatParam(ctx, "pointer_lease", pointer_lease);
        if (!ValidateDuration(ctx, *pointer_lease, "pointer_lease")) return false;
        if (*pointer_lease <= 0.0f)
        {
            RequestSendError(ctx, 400, "bad_request", "pointer_lease must be greater than zero");
            return false;
        }
        return true;
    }

    static void HandlePointerMove(RequestContext* ctx)
    {
        uint64_t input_id = 0;
        float pointer_lease = 0.0f;
        const char* client_id = 0, *session_id = 0;
        if (!PreparePointerCommand(ctx, &input_id, &pointer_lease, &client_id, &session_id)) return;
        InputPoint point;
        memset(&point, 0, sizeof(point));
        if (!RequestGetFloatParam(ctx, "x", &point.m_X) || !RequestGetFloatParam(ctx, "y", &point.m_Y) ||
            !RequestGetFloatParam(ctx, "duration", &point.m_Duration) || !IsFiniteFloat(point.m_X) || !IsFiniteFloat(point.m_Y))
        {
            RequestSendError(ctx, 400, "bad_request", "pointer move requires finite x/y and duration");
            return;
        }
        if (!ValidateDuration(ctx, point.m_Duration, "duration")) return;
        if (!ParseInputEasing(RequestGetParam(ctx, "easing"), &point.m_Easing))
        {
            RequestSendError(ctx, 400, "bad_request", "pointer move easing is unsupported");
            return;
        }
        const char* error = 0;
        if (!AppendPointerMove(input_id, &point, pointer_lease, &error))
        {
            RequestSendError(ctx, 409, "pointer_closed", error);
            return;
        }
        SendReceiptResponse(ctx, FindInputReceipt(input_id), QueuePosition(input_id), 202);
    }

    static void HandlePointerHold(RequestContext* ctx)
    {
        uint64_t input_id = 0;
        float pointer_lease = 0.0f;
        const char* client_id = 0, *session_id = 0;
        if (!PreparePointerCommand(ctx, &input_id, &pointer_lease, &client_id, &session_id)) return;
        float duration = 0.0f;
        if (!RequestGetFloatParam(ctx, "duration", &duration) || !ValidateDuration(ctx, duration, "duration")) return;
        const char* error = 0;
        if (!AppendPointerHold(input_id, duration, pointer_lease, &error))
        {
            RequestSendError(ctx, 409, "pointer_closed", error);
            return;
        }
        SendReceiptResponse(ctx, FindInputReceipt(input_id), QueuePosition(input_id), 202);
    }

    static void HandlePointerUp(RequestContext* ctx)
    {
        uint64_t input_id = 0;
        float pointer_lease = 0.0f;
        const char* client_id = 0, *session_id = 0;
        if (!PreparePointerCommand(ctx, &input_id, &pointer_lease, &client_id, &session_id)) return;
        const char* error = 0;
        if (!ReleasePointer(input_id, &error))
        {
            RequestSendError(ctx, 409, "pointer_closed", error);
            return;
        }
        SendReceiptResponse(ctx, FindInputReceipt(input_id), QueuePosition(input_id), 202);
    }

    static void HandleScreenshot(RequestContext* ctx)
    {
        if (!IsScreenshotSupported())
        {
            RequestSendError(ctx, 501, "screenshot_unsupported", "screenshot capture is not supported by the current graphics adapter");
            return;
        }

        char path[1024];
        if (!BuildScreenshotPath(path, sizeof(path)))
        {
            RequestSendError(ctx, 500, "screenshot_path_failed", "failed to create screenshot path");
            return;
        }
        if (!ScheduleScreenshot(path))
        {
            RequestSendError(ctx, 409, "screenshot_pending", "a screenshot is already pending");
            return;
        }

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"path\":");
        AppendJsonString(&response, path);
        StringBufferAppend(&response, ",\"pending\":true}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void SendMethodNotAllowed(RequestContext* ctx, const char* methods)
    {
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":false,\"error\":{\"code\":");
        AppendJsonString(&response, "method_not_allowed");
        StringBufferAppend(&response, ",\"message\":");
        StringBuffer message;
        StringBufferInit(&message);
        StringBufferAppend(&message, "use ");
        StringBufferAppend(&message, methods);
        char* message_string = StringBufferDetach(&message);
        AppendJsonString(&response, message_string);
        free(message_string);
        StringBufferAppend(&response, "}}\n");
        SendResponse(ctx->m_Request, 405, "application/json", &response, methods);
        StringBufferFree(&response);
    }

    static void AppendAllowedMethod(StringBuffer* methods, const char* method)
    {
        if (methods->m_Size > 0)
        {
            StringBufferAppend(methods, ", ");
        }
        StringBufferAppend(methods, method);
    }

    static const RouteDefinition ROUTES[] = {
        {"/health", "GET", HandleHealth},
        {"/screen", "GET", HandleScreen},
        {"/screen", "PUT", HandleScreenPut},
        {"/scene", "GET", HandleScene},
        {"/nodes", "GET", HandleNodes},
        {"/node", "GET", HandleNode},
        {"/input/click", "POST", HandleClick},
        {"/input/drag", "POST", HandleDrag},
        {"/input/drag_path", "POST", HandleDragPath},
        {"/input/key", "POST", HandleKey},
        {"/input/status", "GET", HandleInputStatus},
        {"/input/pending", "GET", HandleInputPending},
        {"/input/cancel", "POST", HandleInputCancel},
        {"/input/flush", "POST", HandleInputFlush},
        {"/input/configure", "PUT", HandleInputConfigure},
        {"/input/pointer/open", "POST", HandlePointerOpen},
        {"/input/pointer/move", "POST", HandlePointerMove},
        {"/input/pointer/hold", "POST", HandlePointerHold},
        {"/input/pointer/up", "POST", HandlePointerUp},
        {"/screenshot", "GET", HandleScreenshot}
    };

    void AutomationBridgeHandler(void* user_data, dmWebServer::Request* request)
    {
        (void)user_data;

        RequestContext ctx;
        InitRequestContext(&ctx, request);

        if (request->m_ContentLength > 0)
        {
            bool structured_input = StartsWith(ctx.m_Route, "/input/") && !RequestIsMethod(&ctx, "GET");
            if (!structured_input)
            {
                DrainRequestBody(request);
                RequestSendError(&ctx, 400, "body_not_supported", "request body is supported only for mutating input endpoints; use query parameters");
                FreeRequestContext(&ctx);
                return;
            }
            if (request->m_ContentLength > 32768 || ctx.m_Query.m_Count > 0)
            {
                DrainRequestBody(request);
                RequestSendError(&ctx, request->m_ContentLength > 32768 ? 413 : 400,
                                 request->m_ContentLength > 32768 ? "input_too_large" : "bad_request",
                                 request->m_ContentLength > 32768 ? "JSON input body exceeds 32768 bytes" : "do not mix query parameters with a structured JSON body");
                FreeRequestContext(&ctx);
                return;
            }
            char* body = ReceiveRequestBody(request, 32768);
            bool parsed = body && ParseFlatJsonObject(body, &ctx.m_Query);
            free(body);
            if (!parsed)
            {
                RequestSendError(&ctx, 400, "bad_json", "expected a flat JSON object containing string, number, or boolean values");
                FreeRequestContext(&ctx);
                return;
            }
        }

        if (!RequestHasApiPrefix(&ctx))
        {
            RequestSendError(&ctx, 404, "not_found", "endpoint not found");
            FreeRequestContext(&ctx);
            return;
        }

        StringBuffer allowed_methods;
        StringBufferInit(&allowed_methods);
        for (uint32_t i = 0; i < DM_ARRAY_SIZE(ROUTES); ++i)
        {
            const RouteDefinition* route = &ROUTES[i];
            if (!StringsEqual(ctx.m_Route, route->m_Route))
            {
                continue;
            }
            if (!RequestIsMethod(&ctx, route->m_Method))
            {
                AppendAllowedMethod(&allowed_methods, route->m_Method);
                continue;
            }
            StringBufferFree(&allowed_methods);
            route->m_Handler(&ctx);
            FreeRequestContext(&ctx);
            return;
        }

        if (allowed_methods.m_Size > 0)
        {
            char* methods = StringBufferDetach(&allowed_methods);
            SendMethodNotAllowed(&ctx, methods);
            free(methods);
            FreeRequestContext(&ctx);
            return;
        }
        StringBufferFree(&allowed_methods);

        RequestSendError(&ctx, 404, "not_found", "endpoint not found");
        FreeRequestContext(&ctx);
    }

    void RegisterWebEndpoint(dmExtension::AppParams* params)
    {
        g_AutomationBridge.m_WebServer = dmEngine::GetWebServer(params);
        if (!g_AutomationBridge.m_WebServer)
        {
            dmLogWarning("Automation Bridge extension: debug web server is unavailable");
            return;
        }

        dmWebServer::HandlerParams handler_params;
        handler_params.m_Userdata = 0;
        handler_params.m_Handler = AutomationBridgeHandler;

        dmWebServer::Result result = dmWebServer::AddHandler(g_AutomationBridge.m_WebServer, API_PREFIX, &handler_params);
        if (result == dmWebServer::RESULT_OK || result == dmWebServer::RESULT_HANDLER_ALREADY_REGISTRED)
        {
            g_AutomationBridge.m_WebHandlerRegistered = true;
            dmLogInfo("Automation Bridge endpoint registered at `%s`", API_PREFIX);
        }
        else
        {
            dmLogWarning("Unable to register Automation Bridge endpoint '%s' (%d)", API_PREFIX, result);
        }
    }

    void UnregisterWebEndpoint()
    {
        if (!g_AutomationBridge.m_WebServer || !g_AutomationBridge.m_WebHandlerRegistered)
        {
            g_AutomationBridge.m_WebServer = 0;
            g_AutomationBridge.m_WebHandlerRegistered = false;
            return;
        }
        dmWebServer::Result result = dmWebServer::RemoveHandler(g_AutomationBridge.m_WebServer, API_PREFIX);
        if (result != dmWebServer::RESULT_OK && result != dmWebServer::RESULT_HANDLER_NOT_REGISTRED)
        {
            dmLogWarning("Unable to remove Automation Bridge endpoint '%s' (%d)", API_PREFIX, result);
        }
        g_AutomationBridge.m_WebServer = 0;
        g_AutomationBridge.m_WebHandlerRegistered = false;
    }


}

#endif
