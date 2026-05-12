#include "agent_private.h"

#if defined(DM_DEBUG)

#include <ctype.h>
#include <string.h>

namespace dmAgent
{
    struct RequestContext
    {
        dmWebServer::Request*    m_Request;
        char*                    m_Path;
        char*                    m_Route;
        AgentArray<QueryParam>   m_Query;
    };

    typedef void (*RouteHandler)(RequestContext* ctx);

    struct RouteDefinition
    {
        const char*  m_Route;
        const char*  m_Method;
        RouteHandler m_Handler;
    };
    static void SendResponse(dmWebServer::Request* request, int status_code, const char* content_type, const StringBuffer* response)
    {
        const char* data = response->m_Data ? response->m_Data : "";
        dmWebServer::SetStatusCode(request, status_code);
        dmWebServer::SendAttribute(request, "Content-Type", content_type);
        dmWebServer::SendAttribute(request, "Cache-Control", "no-store");
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

    static void HandleHealth(RequestContext* ctx)
    {
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
        StringBufferAppend(&response, ",\"capabilities\":[\"scene\",\"nodes\",\"node\",\"input.click\",\"input.drag\",\"input.key\"");
        if (IsScreenshotSupported())
        {
            StringBufferAppend(&response, ",\"screenshot\"");
        }
        StringBufferAppend(&response, "],\"screen\":");
        AppendScreenJson(&response, &g_Agent.m_Snapshot);
        StringBufferAppend(&response, ",\"scene_sequence\":");
        AppendNumber(&response, (double)g_Agent.m_Snapshot.m_Sequence);
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleScreen(RequestContext* ctx)
    {
        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":");
        AppendScreenJson(&response, &g_Agent.m_Snapshot);
        StringBufferAppend(&response, "}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleScene(RequestContext* ctx)
    {
        IncludeOptions include = ParseInclude(ctx, false, true);
        bool visible_only = false;
        RequestGetBoolParam(ctx, "visible", &visible_only);

        const Snapshot* snapshot = &g_Agent.m_Snapshot;
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
        IncludeOptions include = ParseInclude(ctx, false, false);
        float limit_f = 50.0f;
        RequestGetFloatParam(ctx, "limit", &limit_f);
        uint32_t limit = (uint32_t)ClampFloat(limit_f, 0.0f, 500.0f);

        const Snapshot* snapshot = &g_Agent.m_Snapshot;
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
        AppendNodeJson(&response, &g_Agent.m_Snapshot, node, &include, include.m_Children, false);
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleClick(RequestContext* ctx)
    {
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
        else if (!RequestGetFloatParam(ctx, "x", &x) || !RequestGetFloatParam(ctx, "y", &y))
        {
            RequestSendError(ctx, 400, "bad_request", "provide either id or x/y");
            return;
        }

        if (!AddMouseInput(x, y, x, y, 0.0f))
        {
            RequestSendError(ctx, 429, "input_queue_full", "too many input events are already queued");
            return;
        }

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"queued\":\"click\",\"x\":");
        AppendNumber(&response, x);
        StringBufferAppend(&response, ",\"y\":");
        AppendNumber(&response, y);
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleDrag(RequestContext* ctx)
    {
        float x1 = 0.0f;
        float y1 = 0.0f;
        float x2 = 0.0f;
        float y2 = 0.0f;
        float duration = 0.35f;
        RequestGetFloatParam(ctx, "duration", &duration);

        const char* from_id = RequestGetParam(ctx, "from_id");
        const char* to_id = RequestGetParam(ctx, "to_id");
        if (!IsEmpty(from_id) && !IsEmpty(to_id))
        {
            const char* error = 0;
            if (!GetNodeCenter(from_id, &x1, &y1, &error))
            {
                RequestSendError(ctx, 404, "not_found", error);
                return;
            }
            if (!GetNodeCenter(to_id, &x2, &y2, &error))
            {
                RequestSendError(ctx, 404, "not_found", error);
                return;
            }
        }
        else if (!RequestGetFloatParam(ctx, "x1", &x1) ||
                 !RequestGetFloatParam(ctx, "y1", &y1) ||
                 !RequestGetFloatParam(ctx, "x2", &x2) ||
                 !RequestGetFloatParam(ctx, "y2", &y2))
        {
            RequestSendError(ctx, 400, "bad_request", "provide either from_id/to_id or x1/y1/x2/y2");
            return;
        }

        if (!AddMouseInput(x1, y1, x2, y2, MaxFloat(0.0f, duration)))
        {
            RequestSendError(ctx, 429, "input_queue_full", "too many input events are already queued");
            return;
        }

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"queued\":\"drag\",\"from\":{\"x\":");
        AppendNumber(&response, x1);
        StringBufferAppend(&response, ",\"y\":");
        AppendNumber(&response, y1);
        StringBufferAppend(&response, "},\"to\":{\"x\":");
        AppendNumber(&response, x2);
        StringBufferAppend(&response, ",\"y\":");
        AppendNumber(&response, y2);
        StringBufferAppend(&response, "},\"duration\":");
        AppendNumber(&response, duration);
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 200, &response);
    }

    static void HandleKey(RequestContext* ctx)
    {
        const char* value = RequestGetParam(ctx, "keys");
        if (!value)
        {
            value = RequestGetParam(ctx, "text");
        }
        if (IsEmpty(value))
        {
            RequestSendError(ctx, 400, "bad_request", "provide text or keys");
            return;
        }
        if (strlen(value) > MAX_KEY_INPUT_BYTES)
        {
            RequestSendError(ctx, 413, "input_too_large", "text or keys parameter is too large");
            return;
        }

        if (!AddKeyInput(value))
        {
            RequestSendError(ctx, 429, "input_queue_full", "too many input events are already queued");
            return;
        }

        StringBuffer response;
        StringBufferInit(&response);
        StringBufferAppend(&response, "{\"ok\":true,\"data\":{\"queued\":\"key\",\"length\":");
        AppendNumber(&response, (double)strlen(value));
        StringBufferAppend(&response, "}}\n");
        RequestSendJson(ctx, 200, &response);
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

    static void SendMethodNotAllowed(RequestContext* ctx, const char* method)
    {
        StringBuffer message;
        StringBufferInit(&message);
        StringBufferAppend(&message, "use ");
        StringBufferAppend(&message, method);
        char* message_string = StringBufferDetach(&message);
        RequestSendError(ctx, 405, "method_not_allowed", message_string);
        free(message_string);
    }

    static const RouteDefinition ROUTES[] = {
        {"/health", "GET", HandleHealth},
        {"/screen", "GET", HandleScreen},
        {"/scene", "GET", HandleScene},
        {"/nodes", "GET", HandleNodes},
        {"/node", "GET", HandleNode},
        {"/input/click", "POST", HandleClick},
        {"/input/drag", "POST", HandleDrag},
        {"/input/key", "POST", HandleKey},
        {"/screenshot", "GET", HandleScreenshot}
    };

    void AgentHandler(void* user_data, dmWebServer::Request* request)
    {
        (void)user_data;

        RequestContext ctx;
        InitRequestContext(&ctx, request);

        if (request->m_ContentLength > 0)
        {
            DrainRequestBody(request);
            RequestSendError(&ctx, 400, "body_not_supported", "request body is not supported; use query parameters");
            FreeRequestContext(&ctx);
            return;
        }

        if (!RequestHasApiPrefix(&ctx))
        {
            RequestSendError(&ctx, 404, "not_found", "endpoint not found");
            FreeRequestContext(&ctx);
            return;
        }

        for (uint32_t i = 0; i < DM_ARRAY_SIZE(ROUTES); ++i)
        {
            const RouteDefinition* route = &ROUTES[i];
            if (!StringsEqual(ctx.m_Route, route->m_Route))
            {
                continue;
            }
            if (!RequestIsMethod(&ctx, route->m_Method))
            {
                SendMethodNotAllowed(&ctx, route->m_Method);
                FreeRequestContext(&ctx);
                return;
            }
            route->m_Handler(&ctx);
            FreeRequestContext(&ctx);
            return;
        }

        RequestSendError(&ctx, 404, "not_found", "endpoint not found");
        FreeRequestContext(&ctx);
    }

    void RegisterWebEndpoint(dmExtension::AppParams* params)
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

    void UnregisterWebEndpoint()
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


}

#endif
