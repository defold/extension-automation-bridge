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

    static char* ReadRequestBody(dmWebServer::Request* request)
    {
        char* body = (char*)malloc((size_t)request->m_ContentLength + 1);
        if (!body)
        {
            return 0;
        }
        uint32_t remaining = request->m_ContentLength;
        uint32_t offset = 0;
        while (remaining > 0)
        {
            uint32_t received = 0;
            dmWebServer::Result result = dmWebServer::Receive(request, body + offset, remaining, &received);
            if (result != dmWebServer::RESULT_OK || received == 0)
            {
                free(body);
                return 0;
            }
            remaining -= received;
            offset += received;
        }
        body[offset] = 0;
        return body;
    }

    static void DiscardRequestBody(dmWebServer::Request* request)
    {
        char buffer[512];
        uint32_t remaining = request->m_ContentLength;
        while (remaining > 0)
        {
            uint32_t received = 0;
            uint32_t requested = remaining < sizeof(buffer) ? remaining : (uint32_t)sizeof(buffer);
            if (dmWebServer::Receive(request, buffer, requested, &received) != dmWebServer::RESULT_OK || received == 0)
            {
                return;
            }
            remaining -= received;
        }
    }

    static void JsonSkipWhitespace(const char** cursor)
    {
        while (**cursor && isspace((unsigned char)**cursor))
        {
            ++*cursor;
        }
    }

    static int JsonHexValue(char c)
    {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        return -1;
    }

    static bool JsonAppendCodepoint(StringBuffer* out, uint32_t codepoint)
    {
        if (codepoint <= 0x7f)
        {
            StringBufferAppendChar(out, (char)codepoint);
        }
        else if (codepoint <= 0x7ff)
        {
            StringBufferAppendChar(out, (char)(0xc0 | (codepoint >> 6)));
            StringBufferAppendChar(out, (char)(0x80 | (codepoint & 0x3f)));
        }
        else if (codepoint <= 0xffff)
        {
            StringBufferAppendChar(out, (char)(0xe0 | (codepoint >> 12)));
            StringBufferAppendChar(out, (char)(0x80 | ((codepoint >> 6) & 0x3f)));
            StringBufferAppendChar(out, (char)(0x80 | (codepoint & 0x3f)));
        }
        else if (codepoint <= 0x10ffff)
        {
            StringBufferAppendChar(out, (char)(0xf0 | (codepoint >> 18)));
            StringBufferAppendChar(out, (char)(0x80 | ((codepoint >> 12) & 0x3f)));
            StringBufferAppendChar(out, (char)(0x80 | ((codepoint >> 6) & 0x3f)));
            StringBufferAppendChar(out, (char)(0x80 | (codepoint & 0x3f)));
        }
        else
        {
            return false;
        }
        return !out->m_Failed;
    }

    static bool JsonParseString(const char** cursor, char** value)
    {
        if (**cursor != '"') return false;
        ++*cursor;
        StringBuffer decoded;
        StringBufferInit(&decoded);
        while (**cursor && **cursor != '"')
        {
            unsigned char c = (unsigned char)*(*cursor)++;
            if (c < 0x20)
            {
                StringBufferFree(&decoded);
                return false;
            }
            if (c != '\\')
            {
                StringBufferAppendChar(&decoded, (char)c);
                continue;
            }
            char escaped = *(*cursor)++;
            if (!escaped)
            {
                StringBufferFree(&decoded);
                return false;
            }
            switch (escaped)
            {
            case '"': StringBufferAppendChar(&decoded, '"'); break;
            case '\\': StringBufferAppendChar(&decoded, '\\'); break;
            case '/': StringBufferAppendChar(&decoded, '/'); break;
            case 'b': StringBufferAppendChar(&decoded, '\b'); break;
            case 'f': StringBufferAppendChar(&decoded, '\f'); break;
            case 'n': StringBufferAppendChar(&decoded, '\n'); break;
            case 'r': StringBufferAppendChar(&decoded, '\r'); break;
            case 't': StringBufferAppendChar(&decoded, '\t'); break;
            case 'u':
            {
                uint32_t codepoint = 0;
                for (uint32_t i = 0; i < 4; ++i)
                {
                    int digit = JsonHexValue(*(*cursor)++);
                    if (digit < 0)
                    {
                        StringBufferFree(&decoded);
                        return false;
                    }
                    codepoint = (codepoint << 4) | (uint32_t)digit;
                }
                if (codepoint >= 0xd800 && codepoint <= 0xdbff)
                {
                    if ((*cursor)[0] != '\\' || (*cursor)[1] != 'u')
                    {
                        StringBufferFree(&decoded);
                        return false;
                    }
                    *cursor += 2;
                    uint32_t low = 0;
                    for (uint32_t i = 0; i < 4; ++i)
                    {
                        int digit = JsonHexValue(*(*cursor)++);
                        if (digit < 0)
                        {
                            StringBufferFree(&decoded);
                            return false;
                        }
                        low = (low << 4) | (uint32_t)digit;
                    }
                    if (low < 0xdc00 || low > 0xdfff)
                    {
                        StringBufferFree(&decoded);
                        return false;
                    }
                    codepoint = 0x10000 + ((codepoint - 0xd800) << 10) + (low - 0xdc00);
                }
                else if (codepoint >= 0xdc00 && codepoint <= 0xdfff)
                {
                    StringBufferFree(&decoded);
                    return false;
                }
                if (!JsonAppendCodepoint(&decoded, codepoint))
                {
                    StringBufferFree(&decoded);
                    return false;
                }
                break;
            }
            default:
                StringBufferFree(&decoded);
                return false;
            }
        }
        if (**cursor != '"')
        {
            StringBufferFree(&decoded);
            return false;
        }
        ++*cursor;
        *value = StringBufferDetach(&decoded);
        return *value != 0;
    }

    static bool JsonSkipValue(const char** cursor, uint32_t depth);

    static bool JsonPushParam(Array<QueryParam>* params, const char* key, char* value)
    {
        QueryParam param;
        memset(&param, 0, sizeof(param));
        param.m_Key = DuplicateString(key);
        param.m_Value = value;
        if (!param.m_Key || !param.m_Value || !ArrayPush(params, &param))
        {
            FreeString(&param.m_Key);
            FreeString(&param.m_Value);
            return false;
        }
        return true;
    }

    static bool JsonSkipArray(const char** cursor, uint32_t depth)
    {
        if (depth > MAX_JSON_NESTING || **cursor != '[') return false;
        ++*cursor;
        JsonSkipWhitespace(cursor);
        if (**cursor == ']') { ++*cursor; return true; }
        while (**cursor)
        {
            if (!JsonSkipValue(cursor, depth + 1)) return false;
            JsonSkipWhitespace(cursor);
            if (**cursor == ']') { ++*cursor; return true; }
            if (**cursor != ',') return false;
            ++*cursor;
            JsonSkipWhitespace(cursor);
        }
        return false;
    }

    static bool JsonSkipObject(const char** cursor, uint32_t depth)
    {
        if (depth > MAX_JSON_NESTING || **cursor != '{') return false;
        ++*cursor;
        JsonSkipWhitespace(cursor);
        if (**cursor == '}') { ++*cursor; return true; }
        while (**cursor)
        {
            char* key = 0;
            if (!JsonParseString(cursor, &key)) return false;
            free(key);
            JsonSkipWhitespace(cursor);
            if (**cursor != ':') return false;
            ++*cursor;
            JsonSkipWhitespace(cursor);
            if (!JsonSkipValue(cursor, depth + 1)) return false;
            JsonSkipWhitespace(cursor);
            if (**cursor == '}') { ++*cursor; return true; }
            if (**cursor != ',') return false;
            ++*cursor;
            JsonSkipWhitespace(cursor);
        }
        return false;
    }

    static bool JsonSkipValue(const char** cursor, uint32_t depth)
    {
        if (depth > MAX_JSON_NESTING) return false;
        JsonSkipWhitespace(cursor);
        if (**cursor == '{') return JsonSkipObject(cursor, depth);
        if (**cursor == '[') return JsonSkipArray(cursor, depth);
        if (**cursor == '"')
        {
            char* value = 0;
            bool ok = JsonParseString(cursor, &value);
            free(value);
            return ok;
        }
        if (StartsWith(*cursor, "true")) { *cursor += 4; return true; }
        if (StartsWith(*cursor, "false")) { *cursor += 5; return true; }
        if (StartsWith(*cursor, "null")) { *cursor += 4; return true; }
        const char* number = *cursor;
        if (*number == '-') ++number;
        if (*number == '0')
        {
            ++number;
            if (isdigit((unsigned char)*number)) return false;
        }
        else
        {
            if (*number < '1' || *number > '9') return false;
            while (isdigit((unsigned char)*number)) ++number;
        }
        if (*number == '.')
        {
            ++number;
            if (!isdigit((unsigned char)*number)) return false;
            while (isdigit((unsigned char)*number)) ++number;
        }
        if (*number == 'e' || *number == 'E')
        {
            ++number;
            if (*number == '+' || *number == '-') ++number;
            if (!isdigit((unsigned char)*number)) return false;
            while (isdigit((unsigned char)*number)) ++number;
        }
        *cursor = number;
        return true;
    }

    static bool JsonParsePointObject(const char** cursor, Array<QueryParam>* params)
    {
        if (**cursor != '{') return false;
        ++*cursor;
        JsonSkipWhitespace(cursor);
        if (**cursor == '}') { ++*cursor; return true; }
        while (**cursor)
        {
            char* key = 0;
            if (!JsonParseString(cursor, &key)) return false;
            JsonSkipWhitespace(cursor);
            if (**cursor != ':') { free(key); return false; }
            ++*cursor;
            JsonSkipWhitespace(cursor);
            if ((StringsEqual(key, "x") || StringsEqual(key, "y")) && **cursor != '{' && **cursor != '[')
            {
                char* value = 0;
                if (**cursor == '"')
                {
                    if (!JsonParseString(cursor, &value)) { free(key); return false; }
                }
                else
                {
                    const char* start = *cursor;
                    if (!JsonSkipValue(cursor, 2)) { free(key); return false; }
                    value = DuplicateStringN(start, (uint32_t)(*cursor - start));
                }
                char flattened_key[16];
                dmSnPrintf(flattened_key, sizeof(flattened_key), "point.%s", key);
                if (!JsonPushParam(params, flattened_key, value)) { free(key); return false; }
            }
            else if (!JsonSkipValue(cursor, 2))
            {
                free(key);
                return false;
            }
            free(key);
            JsonSkipWhitespace(cursor);
            if (**cursor == '}') { ++*cursor; return true; }
            if (**cursor != ',') return false;
            ++*cursor;
            JsonSkipWhitespace(cursor);
        }
        return false;
    }

    static bool JsonParseRootObject(const char* body, Array<QueryParam>* params)
    {
        const char* cursor = body;
        JsonSkipWhitespace(&cursor);
        if (*cursor != '{') return false;
        ++cursor;
        JsonSkipWhitespace(&cursor);
        if (*cursor == '}') { ++cursor; JsonSkipWhitespace(&cursor); return *cursor == 0; }
        while (*cursor)
        {
            QueryParam param;
            memset(&param, 0, sizeof(param));
            if (!JsonParseString(&cursor, &param.m_Key)) return false;
            JsonSkipWhitespace(&cursor);
            if (*cursor != ':') { FreeString(&param.m_Key); return false; }
            ++cursor;
            JsonSkipWhitespace(&cursor);

            if (*cursor == '"')
            {
                if (!JsonParseString(&cursor, &param.m_Value)) { FreeString(&param.m_Key); return false; }
            }
            else if (*cursor == '{' || *cursor == '[')
            {
                if (StringsEqual(param.m_Key, "point") && *cursor == '{')
                {
                    if (!JsonParsePointObject(&cursor, params)) { FreeString(&param.m_Key); return false; }
                }
                else if (!JsonSkipValue(&cursor, 1)) { FreeString(&param.m_Key); return false; }
            }
            else
            {
                const char* start = cursor;
                if (!JsonSkipValue(&cursor, 1)) { FreeString(&param.m_Key); return false; }
                if ((uint32_t)(cursor - start) != 4 || strncmp(start, "null", 4) != 0)
                {
                    param.m_Value = DuplicateStringN(start, (uint32_t)(cursor - start));
                }
            }

            if (param.m_Value)
            {
                if (!ArrayPush(params, &param))
                {
                    FreeString(&param.m_Key);
                    FreeString(&param.m_Value);
                    return false;
                }
            }
            else
            {
                FreeString(&param.m_Key);
            }
            JsonSkipWhitespace(&cursor);
            if (*cursor == '}')
            {
                ++cursor;
                JsonSkipWhitespace(&cursor);
                return *cursor == 0;
            }
            if (*cursor != ',') return false;
            ++cursor;
            JsonSkipWhitespace(&cursor);
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

    static bool RequestGetUIntParamAllowZero(const RequestContext* ctx, const char* key, uint32_t* value, uint32_t max_value = 0xffffffffU)
    {
        const char* text = GetParam(&ctx->m_Query, key);
        if (IsEmpty(text) || !isdigit((unsigned char)text[0]))
        {
            return false;
        }
        char* end = 0;
        unsigned long parsed = strtoul(text, &end, 10);
        if (!end || *end != 0 || parsed > (unsigned long)max_value)
        {
            return false;
        }
        *value = (uint32_t)parsed;
        return true;
    }

    static bool RequestGetUInt64Param(const RequestContext* ctx, const char* key, uint64_t* value,
                                      uint64_t max_value = 0xffffffffffffffffULL, bool allow_zero = true)
    {
        const char* text = GetParam(&ctx->m_Query, key);
        if (IsEmpty(text) || !isdigit((unsigned char)text[0]))
        {
            return false;
        }
        char* end = 0;
        unsigned long long parsed = strtoull(text, &end, 10);
        if (!end || *end != 0 || parsed > max_value || (!allow_zero && parsed == 0))
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
        if (!RequestGetUInt64Param(ctx, "expected_scene_sequence", &expected) || expected == 0)
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
        StringBufferAppend(&response, ",\"capabilities\":[\"scene\",\"nodes\",\"node\",\"events.cursor\",\"events.long_poll\",\"timeline.markers\"");
        if (DefoldPrivateApiCanSetWindowSize())
        {
            StringBufferAppend(&response, ",\"screen.resize\"");
        }
        StringBufferAppend(&response, ",\"input.click\",\"input.drag\",\"input.drag_path\",\"input.pointer\",\"input.key\",\"input.receipts\",\"input.queue\",\"input.controller\",\"input.device.mouse\"");
        if (IsInputDeviceSupported(INPUT_DEVICE_TOUCH))
        {
            StringBufferAppend(&response, ",\"input.device.touch\"");
        }
        if (g_AutomationBridge.m_ApplicationApiEnabled)
        {
            StringBufferAppend(&response, ",\"application.events\",\"application.state\",\"application.commands\",\"application.acknowledgements\",\"application.annotations\"");
        }
        if (IsScreenshotSupported())
        {
            StringBufferAppend(&response, ",\"screenshot\"");
        }
        StringBufferAppend(&response, ",\"scene.pagination\",\"scene.identity\",\"coordinates.convert\",\"frame.wait\"");
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
        StringBufferAppend(&response, ",\"application_api_enabled\":");
        StringBufferAppend(&response, g_AutomationBridge.m_ApplicationApiEnabled ? "true" : "false");
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
        response.m_Size--;
        response.m_Data[response.m_Size] = 0;
        StringBufferAppend(&response, ",\"scene_sequence\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Snapshot.m_Sequence);
        StringBufferAppend(&response, ",\"engine_frame\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Frame);
        StringBufferAppend(&response, "}}\n");
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

        if (!DefoldPrivateApiCanSetWindowSize())
        {
            RequestSendError(ctx, 409, "screen_resize_unsupported", "the platform window cannot be resized");
            return;
        }

        RefreshSnapshotForRequest();
        bool already_correct = g_AutomationBridge.m_Snapshot.m_WindowWidth == width &&
                               g_AutomationBridge.m_Snapshot.m_WindowHeight == height;
        bool observed = already_correct || DefoldPrivateApiSetWindowSize(width, height);

        UpdateSnapshot();
        g_AutomationBridge.m_SnapshotFrame = g_AutomationBridge.m_Frame;

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"requested\":{\"width\":");
        AppendNumber(&response, width);
        StringBufferAppend(&response, ",\"height\":");
        AppendNumber(&response, height);
        StringBufferAppend(&response, "},\"outcome\":");
        AppendJsonString(&response, already_correct ? "already_correct" : (observed ? "resized" : "requested_not_observed"));
        StringBufferAppend(&response, ",\"window_matches\":");
        StringBufferAppend(&response, g_AutomationBridge.m_Snapshot.m_WindowWidth == width && g_AutomationBridge.m_Snapshot.m_WindowHeight == height ? "true" : "false");
        StringBufferAppend(&response, ",\"backbuffer_matches\":");
        StringBufferAppend(&response, g_AutomationBridge.m_Snapshot.m_BackbufferWidth == width && g_AutomationBridge.m_Snapshot.m_BackbufferHeight == height ? "true" : "false");
        StringBufferAppend(&response, ",\"screen\":");
        AppendScreenJson(&response, &g_AutomationBridge.m_Snapshot);
        StringBufferAppend(&response, ",\"scene_sequence\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Snapshot.m_Sequence);
        StringBufferAppend(&response, ",\"engine_frame\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Frame);
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, observed ? 200 : 202, &response);
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
        StringBufferAppend(&response, ",\"scene_sequence\":");
        AppendNumber(&response, (double)snapshot->m_Sequence);
        StringBufferAppend(&response, ",\"engine_frame\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Frame);
        StringBufferAppend(&response, ",\"root\":");
        AppendNodeJson(&response, snapshot, &snapshot->m_Nodes.m_Data[snapshot->m_Root], &include, true, visible_only);
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static bool NodeMatchesFilters(const Node* node, const RequestContext* ctx, bool ignore_state_filters = false)
    {
        bool case_sensitive = false;
        RequestGetBoolParam(ctx, "case_sensitive", &case_sensitive);
        const char* value = RequestGetParam(ctx, "id");
        if (value && !StringsEqual(node->m_Id, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "type");
        if (value && !(case_sensitive ? ContainsCaseSensitive(node->m_Type, value) : ContainsCaseInsensitive(node->m_Type, value)))
        {
            return false;
        }
        value = RequestGetParam(ctx, "type_exact");
        if (value && !StringsEqual(node->m_Type, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "name");
        if (value && !(case_sensitive ? ContainsCaseSensitive(node->m_Name, value) : ContainsCaseInsensitive(node->m_Name, value)))
        {
            return false;
        }
        value = RequestGetParam(ctx, "name_exact");
        if (value && !StringsEqual(node->m_Name, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "text");
        if (value && !(case_sensitive ? ContainsCaseSensitive(node->m_Text, value) : ContainsCaseInsensitive(node->m_Text, value)))
        {
            return false;
        }
        value = RequestGetParam(ctx, "text_exact");
        if (value && !StringsEqual(node->m_Text, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "url");
        if (value && !(case_sensitive ? ContainsCaseSensitive(node->m_Url, value) : ContainsCaseInsensitive(node->m_Url, value)))
        {
            return false;
        }
        value = RequestGetParam(ctx, "url_exact");
        if (value && !StringsEqual(node->m_Url, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "automation_id");
        if (value && !StringsEqual(node->m_AutomationId, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "localization_key");
        if (value && !StringsEqual(node->m_LocalizationKey, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "role");
        if (value && !StringsEqual(node->m_Role, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "path");
        if (value && !StringsEqual(node->m_Path, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "kind");
        if (value && !StringsEqual(node->m_Kind, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "instance_id");
        if (value && !StringsEqual(node->m_InstanceId, value))
        {
            return false;
        }
        value = RequestGetParam(ctx, "logical_id");
        if (value && !StringsEqual(node->m_LogicalId, value))
        {
            return false;
        }
        if (!ignore_state_filters)
        {
            bool visible = false;
            if (RequestGetBoolParam(ctx, "visible", &visible) && node->m_Visible != visible)
            {
                return false;
            }
            bool enabled = false;
            if (RequestGetBoolParam(ctx, "enabled", &enabled) && node->m_Enabled != enabled)
            {
                return false;
            }
            bool has_bounds = false;
            if (RequestGetBoolParam(ctx, "has_bounds", &has_bounds) && node->m_Bounds.m_Valid != has_bounds)
            {
                return false;
            }
            bool visible_and_enabled = false;
            if (RequestGetBoolParam(ctx, "visible_and_enabled", &visible_and_enabled) &&
                (node->m_Visible && node->m_Enabled) != visible_and_enabled)
            {
                return false;
            }
        }
        return true;
    }

    static void AppendActiveCollectionsJson(StringBuffer* response, const Snapshot* snapshot)
    {
        StringBufferAppend(response, "[");
        uint32_t emitted = 0;
        for (uint32_t i = 0; i < snapshot->m_Nodes.m_Count; ++i)
        {
            const Node* node = &snapshot->m_Nodes.m_Data[i];
            if (!StringsEqual(node->m_Kind, "collection") || !node->m_Visible)
            {
                continue;
            }
            if (emitted++)
            {
                StringBufferAppendChar(response, ',');
            }
            AppendJsonString(response, node->m_Name);
        }
        StringBufferAppend(response, "]");
    }

    static void HandleNodes(RequestContext* ctx)
    {
        RefreshSnapshotForRequest();

        IncludeOptions include = ParseInclude(ctx, false, false);
        uint32_t limit = 50;
        RequestGetUIntParamAllowZero(ctx, "limit", &limit, 500);
        uint32_t offset = 0;
        if (!RequestGetUIntParamAllowZero(ctx, "cursor", &offset))
        {
            RequestGetUIntParamAllowZero(ctx, "offset", &offset);
        }

        const Snapshot* snapshot = &g_AutomationBridge.m_Snapshot;
        uint32_t matched = 0;
        uint32_t emitted = 0;
        uint32_t excluded_visibility = 0;
        uint32_t excluded_enabled = 0;
        uint32_t excluded_bounds = 0;

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"nodes\":[");
        for (uint32_t i = 0; i < snapshot->m_Nodes.m_Count; ++i)
        {
            const Node* node = &snapshot->m_Nodes.m_Data[i];
            if (!NodeMatchesFilters(node, ctx))
            {
                if (NodeMatchesFilters(node, ctx, true))
                {
                    bool expected = false;
                    if (RequestGetBoolParam(ctx, "visible", &expected) && node->m_Visible != expected) ++excluded_visibility;
                    if (RequestGetBoolParam(ctx, "enabled", &expected) && node->m_Enabled != expected) ++excluded_enabled;
                    if (RequestGetBoolParam(ctx, "has_bounds", &expected) && node->m_Bounds.m_Valid != expected) ++excluded_bounds;
                    if (RequestGetBoolParam(ctx, "visible_and_enabled", &expected) && (node->m_Visible && node->m_Enabled) != expected)
                    {
                        if (node->m_Visible != expected) ++excluded_visibility;
                        if (node->m_Enabled != expected) ++excluded_enabled;
                    }
                }
                continue;
            }
            ++matched;
            if (matched <= offset)
            {
                continue;
            }
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
        bool truncated = limit > 0 && offset + emitted < matched;
        StringBufferAppend(&response, ",\"offset\":");
        AppendNumber(&response, offset);
        StringBufferAppend(&response, ",\"truncated\":");
        StringBufferAppend(&response, truncated ? "true" : "false");
        StringBufferAppend(&response, ",\"next_cursor\":");
        if (truncated)
        {
            char cursor[32];
            dmSnPrintf(cursor, sizeof(cursor), "%u", offset + emitted);
            AppendJsonString(&response, cursor);
        }
        else
        {
            StringBufferAppend(&response, "null");
        }
        StringBufferAppend(&response, ",\"scene_sequence\":");
        AppendNumber(&response, (double)snapshot->m_Sequence);
        StringBufferAppend(&response, ",\"engine_frame\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Frame);
        StringBufferAppend(&response, ",\"active_collections\":");
        AppendActiveCollectionsJson(&response, snapshot);
        StringBufferAppend(&response, ",\"excluded\":{\"visibility\":");
        AppendNumber(&response, excluded_visibility);
        StringBufferAppend(&response, ",\"enabled\":");
        AppendNumber(&response, excluded_enabled);
        StringBufferAppend(&response, ",\"bounds\":");
        AppendNumber(&response, excluded_bounds);
        StringBufferAppend(&response, "}");
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
        StringBufferAppend(&response, ",\"scene_sequence\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Snapshot.m_Sequence);
        StringBufferAppend(&response, ",\"engine_frame\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Frame);
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
            if (!RequestGetUInt64Param(ctx, "pointer_id", &pointer_value, 0xffffffffU))
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
        if (!RequestGetUInt64Param(ctx, "input_id", input_id) || *input_id == 0)
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

    static bool PointToBackbuffer(const Snapshot* snapshot, const char* space, float x, float y, float* out_x, float* out_y)
    {
        float window_w = (float)snapshot->m_WindowWidth;
        float window_h = (float)snapshot->m_WindowHeight;
        float backbuffer_w = (float)snapshot->m_BackbufferWidth;
        float backbuffer_h = (float)snapshot->m_BackbufferHeight;
        float viewport_top = backbuffer_h - (float)snapshot->m_ViewportY - (float)snapshot->m_ViewportHeight;
        if (window_w <= 0.0f || window_h <= 0.0f || backbuffer_w <= 0.0f || backbuffer_h <= 0.0f)
        {
            return false;
        }
        if (StringsEqual(space, "window") || StringsEqual(space, "client") || StringsEqual(space, "display_pixels"))
        {
            *out_x = x * backbuffer_w / window_w;
            *out_y = y * backbuffer_h / window_h;
            return true;
        }
        if (StringsEqual(space, "backbuffer"))
        {
            *out_x = x;
            *out_y = y;
            return true;
        }
        if (StringsEqual(space, "viewport"))
        {
            *out_x = (float)snapshot->m_ViewportX + x;
            *out_y = viewport_top + y;
            return true;
        }
        if (StringsEqual(space, "normalized_viewport"))
        {
            *out_x = (float)snapshot->m_ViewportX + x * (float)snapshot->m_ViewportWidth;
            *out_y = viewport_top + y * (float)snapshot->m_ViewportHeight;
            return true;
        }
        return false;
    }

    static bool PointFromBackbuffer(const Snapshot* snapshot, const char* space, float x, float y, float* out_x, float* out_y)
    {
        float window_w = (float)snapshot->m_WindowWidth;
        float window_h = (float)snapshot->m_WindowHeight;
        float backbuffer_w = (float)snapshot->m_BackbufferWidth;
        float backbuffer_h = (float)snapshot->m_BackbufferHeight;
        float viewport_top = backbuffer_h - (float)snapshot->m_ViewportY - (float)snapshot->m_ViewportHeight;
        if (window_w <= 0.0f || window_h <= 0.0f || backbuffer_w <= 0.0f || backbuffer_h <= 0.0f)
        {
            return false;
        }
        if (StringsEqual(space, "window") || StringsEqual(space, "client") || StringsEqual(space, "display_pixels"))
        {
            *out_x = x * window_w / backbuffer_w;
            *out_y = y * window_h / backbuffer_h;
            return true;
        }
        if (StringsEqual(space, "backbuffer"))
        {
            *out_x = x;
            *out_y = y;
            return true;
        }
        if (StringsEqual(space, "viewport"))
        {
            *out_x = x - (float)snapshot->m_ViewportX;
            *out_y = y - viewport_top;
            return true;
        }
        if (StringsEqual(space, "normalized_viewport") && snapshot->m_ViewportWidth > 0 && snapshot->m_ViewportHeight > 0)
        {
            *out_x = (x - (float)snapshot->m_ViewportX) / (float)snapshot->m_ViewportWidth;
            *out_y = (y - viewport_top) / (float)snapshot->m_ViewportHeight;
            return true;
        }
        return false;
    }

    static void HandleCoordinateConvert(RequestContext* ctx)
    {
        RefreshSnapshotForRequest();
        float x = 0.0f;
        float y = 0.0f;
        const char* from_space = RequestGetParam(ctx, "from_space");
        const char* to_space = RequestGetParam(ctx, "to_space");
        bool has_x = RequestGetFloatParam(ctx, "point.x", &x) || RequestGetFloatParam(ctx, "x", &x);
        bool has_y = RequestGetFloatParam(ctx, "point.y", &y) || RequestGetFloatParam(ctx, "y", &y);
        if (!has_x || !has_y || IsEmpty(from_space) || IsEmpty(to_space))
        {
            RequestSendError(ctx, 400, "bad_request", "provide numeric x/y and named from_space/to_space");
            return;
        }
        float backbuffer_x = 0.0f;
        float backbuffer_y = 0.0f;
        float converted_x = 0.0f;
        float converted_y = 0.0f;
        const Snapshot* snapshot = &g_AutomationBridge.m_Snapshot;
        if (!PointToBackbuffer(snapshot, from_space, x, y, &backbuffer_x, &backbuffer_y) ||
            !PointFromBackbuffer(snapshot, to_space, backbuffer_x, backbuffer_y, &converted_x, &converted_y))
        {
            RequestSendError(ctx, 400, "unsupported_coordinate_space", "supported spaces are window, client, display_pixels, backbuffer, viewport, and normalized_viewport");
            return;
        }

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"point\":{\"x\":");
        AppendNumber(&response, converted_x);
        StringBufferAppend(&response, ",\"y\":");
        AppendNumber(&response, converted_y);
        StringBufferAppend(&response, "},\"from_space\":");
        AppendJsonString(&response, from_space);
        StringBufferAppend(&response, ",\"to_space\":");
        AppendJsonString(&response, to_space);
        StringBufferAppend(&response, ",\"scene_sequence\":");
        AppendNumber(&response, (double)snapshot->m_Sequence);
        StringBufferAppend(&response, ",\"engine_frame\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Frame);
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleFrame(RequestContext* ctx)
    {
        RefreshSnapshotForRequest();
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"engine_frame\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Frame);
        StringBufferAppend(&response, ",\"scene_sequence\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Snapshot.m_Sequence);
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
        RefreshSnapshotForRequest();
        if (!IsScreenshotSupported())
        {
            RequestSendError(ctx, 501, "screenshot_unsupported", "screenshot capture is not supported by the current graphics adapter");
            return;
        }

        uint32_t after_frames = 0;
        RequestGetUIntParamAllowZero(ctx, "after_frames", &after_frames, 600);
        ScreenshotCapture capture;
        if (!ScheduleScreenshot(after_frames, &capture))
        {
            RequestSendError(ctx, 409, "screenshot_pending", "a screenshot is already pending");
            return;
        }

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":");
        AppendScreenshotJson(&response, &capture);
        response.m_Size--;
        response.m_Data[response.m_Size] = 0;
        StringBufferAppend(&response, ",\"accepted_frame\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Frame);
        StringBufferAppend(&response, ",\"accepted_scene_sequence\":");
        AppendNumber(&response, (double)g_AutomationBridge.m_Snapshot.m_Sequence);
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleScreenshotStatus(RequestContext* ctx)
    {
        uint64_t capture_id = 0;
        if (!RequestGetUInt64Param(ctx, "capture_id", &capture_id) && !RequestGetUInt64Param(ctx, "id", &capture_id))
        {
            RequestSendError(ctx, 400, "bad_request", "provide capture_id");
            return;
        }
        const ScreenshotCapture* capture = FindScreenshotCapture(capture_id);
        if (!capture)
        {
            RequestSendError(ctx, 404, "screenshot_not_found", "screenshot capture id was not found");
            return;
        }
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":");
        AppendScreenshotJson(&response, capture);
        StringBufferAppend(&response, "}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleEventCursor(RequestContext* ctx)
    {
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"cursor\":");
        AppendNumber(&response, (double)GetEventNextCursor());
        StringBufferAppend(&response, ",\"oldest_cursor\":");
        AppendNumber(&response, (double)GetEventOldestCursor());
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleEvents(RequestContext* ctx)
    {
        uint64_t cursor = GetEventOldestCursor();
        const char* cursor_text = RequestGetParam(ctx, "cursor");
        if (!IsEmpty(cursor_text) && !RequestGetUInt64Param(ctx, "cursor", &cursor))
        {
            RequestSendError(ctx, 400, "bad_cursor", "cursor must be an unsigned integer");
            return;
        }
        uint64_t timeout_ms_64 = 0;
        const char* timeout_text = RequestGetParam(ctx, "timeout_ms");
        if (!IsEmpty(timeout_text) && !RequestGetUInt64Param(ctx, "timeout_ms", &timeout_ms_64, 30000))
        {
            RequestSendError(ctx, 400, "bad_timeout", "timeout_ms must be between 0 and 30000");
            return;
        }
        uint64_t limit_64 = 100;
        const char* limit_text = RequestGetParam(ctx, "limit");
        if (!IsEmpty(limit_text) && (!RequestGetUInt64Param(ctx, "limit", &limit_64, 256) || limit_64 == 0))
        {
            RequestSendError(ctx, 400, "bad_limit", "limit must be between 1 and 256");
            return;
        }

        uint64_t deadline = dmTime::GetTime() + timeout_ms_64 * 1000;
        StringBuffer page;
        uint32_t count = 0;
        bool overflow = false;
        uint64_t next_cursor = cursor;
        do
        {
            StringBufferInit(&page);
            count = AppendEventPageJson(&page, cursor, (uint32_t)limit_64, &overflow, &next_cursor);
            if (count || overflow || dmTime::GetTime() >= deadline) break;
            StringBufferFree(&page);
            dmTime::Sleep(10000);
        } while (true);

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":");
        StringBufferAppend(&response, page.m_Data);
        StringBufferAppend(&response, "}\n");
        StringBufferFree(&page);
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleState(RequestContext* ctx)
    {
        StringBuffer page;
        StringBufferInit(&page);
        AppendPublishedStatesJson(&page, RequestGetParam(ctx, "name"), 0);
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":");
        StringBufferAppend(&response, page.m_Data);
        StringBufferAppend(&response, "}\n");
        StringBufferFree(&page);
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleStateWait(RequestContext* ctx)
    {
        uint64_t after_revision = 0;
        if (!RequestGetUInt64Param(ctx, "after_revision", &after_revision))
        {
            RequestSendError(ctx, 400, "bad_revision", "after_revision is required and must be an unsigned integer");
            return;
        }
        uint64_t timeout_ms = 0;
        const char* timeout_text = RequestGetParam(ctx, "timeout_ms");
        if (!IsEmpty(timeout_text) && !RequestGetUInt64Param(ctx, "timeout_ms", &timeout_ms, 30000))
        {
            RequestSendError(ctx, 400, "bad_timeout", "timeout_ms must be between 0 and 30000");
            return;
        }
        uint64_t deadline = dmTime::GetTime() + timeout_ms * 1000;
        StringBuffer page;
        uint32_t count = 0;
        do
        {
            StringBufferInit(&page);
            count = AppendPublishedStatesJson(&page, RequestGetParam(ctx, "name"), after_revision);
            if (count || dmTime::GetTime() >= deadline) break;
            StringBufferFree(&page);
            dmTime::Sleep(10000);
        } while (true);
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":");
        StringBufferAppend(&response, page.m_Data);
        StringBufferAppend(&response, "}\n");
        StringBufferFree(&page);
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleCommandSubmit(RequestContext* ctx)
    {
        const char* name = RequestGetParam(ctx, "name");
        const char* data = RequestGetParam(ctx, "data");
        if (!data) data = "{}";
        uint64_t timeout_ms = 30000;
        const char* timeout_text = RequestGetParam(ctx, "timeout_ms");
        if (!IsEmpty(timeout_text) && (!RequestGetUInt64Param(ctx, "timeout_ms", &timeout_ms, 300000) || timeout_ms == 0))
        {
            RequestSendError(ctx, 400, "bad_timeout", "timeout_ms must be between 1 and 300000");
            return;
        }
        uint64_t command_id = 0;
        const char* error = 0;
        if (!SubmitCommand(name, data, (uint32_t)timeout_ms, &command_id, &error))
        {
            RequestSendError(ctx, 400, "command_rejected", error);
            return;
        }
        StringBuffer event_data;
        StringBufferInit(&event_data);
        StringBufferAppend(&event_data, "{\"command_id\":"); AppendNumber(&event_data, (double)command_id);
        StringBufferAppend(&event_data, ",\"name\":"); AppendJsonString(&event_data, name);
        StringBufferAppend(&event_data, "}");
        EmitBridgeEvent("command", "command.accepted", event_data.m_Data, 0, false);
        StringBufferFree(&event_data);

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"command_id\":");
        AppendNumber(&response, (double)command_id);
        StringBufferAppend(&response, ",\"state\":\"pending\"}}\n");
        RequestSendJson(ctx, 202, &response);
    }

    static void HandleCommandStatus(RequestContext* ctx)
    {
        uint64_t command_id = 0;
        if (!RequestGetUInt64Param(ctx, "id", &command_id) || command_id == 0)
        {
            RequestSendError(ctx, 400, "bad_command_id", "id must be a positive integer");
            return;
        }
        StringBuffer command;
        StringBufferInit(&command);
        if (!AppendCommandJson(&command, command_id))
        {
            StringBufferFree(&command);
            RequestSendError(ctx, 404, "command_not_found", "command id was not found");
            return;
        }
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":");
        StringBufferAppend(&response, command.m_Data);
        StringBufferAppend(&response, "}\n");
        StringBufferFree(&command);
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleCommandCancel(RequestContext* ctx)
    {
        uint64_t command_id = 0;
        if (!RequestGetUInt64Param(ctx, "id", &command_id) || command_id == 0)
        {
            RequestSendError(ctx, 400, "bad_command_id", "id must be a positive integer");
            return;
        }
        const char* error = 0;
        if (!CancelCommand(command_id, &error))
        {
            RequestSendError(ctx, 409, "command_not_cancellable", error);
            return;
        }
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"command_id\":");
        AppendNumber(&response, (double)command_id);
        StringBufferAppend(&response, ",\"state\":\"cancelled\"}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleMarker(RequestContext* ctx)
    {
        const char* name = RequestGetParam(ctx, "name");
        const char* data = RequestGetParam(ctx, "data");
        if (!data) data = "{}";
        uint64_t recording_timestamp_us = 0;
        bool has_recording_timestamp = RequestGetUInt64Param(ctx, "recording_timestamp_us", &recording_timestamp_us);
        uint64_t event_sequence = 0;
        uint64_t native_timestamp_us = 0;
        if (!AddTimelineMarker(name, data, recording_timestamp_us, has_recording_timestamp, &event_sequence, &native_timestamp_us))
        {
            RequestSendError(ctx, 400, "marker_rejected", "marker name or JSON data is invalid");
            return;
        }
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"event_sequence\":");
        AppendNumber(&response, (double)event_sequence);
        StringBufferAppend(&response, ",\"name\":"); AppendJsonString(&response, name);
        StringBufferAppend(&response, ",\"native_timestamp_us\":"); AppendNumber(&response, (double)native_timestamp_us);
        StringBufferAppend(&response, ",\"recording_timestamp_us\":");
        if (has_recording_timestamp) AppendNumber(&response, (double)recording_timestamp_us); else StringBufferAppend(&response, "null");
        StringBufferAppend(&response, "}}\n");
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
        {"/coordinates/convert", "POST", HandleCoordinateConvert},
        {"/frame", "GET", HandleFrame},
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
        {"/screenshot", "GET", HandleScreenshot},
        {"/events/cursor", "GET", HandleEventCursor},
        {"/events", "GET", HandleEvents},
        {"/state", "GET", HandleState},
        {"/state/wait", "GET", HandleStateWait},
        {"/commands", "POST", HandleCommandSubmit},
        {"/commands", "GET", HandleCommandStatus},
        {"/commands", "DELETE", HandleCommandCancel},
        {"/markers", "POST", HandleMarker},
        {"/screenshot/status", "GET", HandleScreenshotStatus}
    };

    void AutomationBridgeHandler(void* user_data, dmWebServer::Request* request)
    {
        (void)user_data;

        RequestContext ctx;
        InitRequestContext(&ctx, request);

        if (request->m_ContentLength > 0)
        {
            if (request->m_ContentLength > MAX_JSON_REQUEST_BYTES)
            {
                DiscardRequestBody(request);
                RequestSendError(&ctx, 413, "json_body_too_large", "JSON request body exceeds the 65536-byte limit");
                FreeRequestContext(&ctx);
                return;
            }
            const char* content_type = dmWebServer::GetHeader(request, "Content-Type");
            if (!content_type || !StartsWith(content_type, "application/json"))
            {
                free(ReadRequestBody(request));
                RequestSendError(&ctx, 415, "unsupported_media_type", "request bodies must use application/json");
                FreeRequestContext(&ctx);
                return;
            }
            if (RequestIsMethod(&ctx, "GET"))
            {
                free(ReadRequestBody(request));
                RequestSendError(&ctx, 400, "body_not_supported", "GET endpoints do not accept request bodies");
                FreeRequestContext(&ctx);
                return;
            }
            char* body = ReadRequestBody(request);
            bool parsed = body && JsonParseRootObject(body, &ctx.m_Query);
            free(body);
            if (!parsed)
            {
                RequestSendError(&ctx, 400, "invalid_json", "request body must be a valid JSON object with at most 16 nesting levels");
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
