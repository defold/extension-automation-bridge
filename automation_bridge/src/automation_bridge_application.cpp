#include "automation_bridge_private.h"

#if defined(DM_DEBUG)

#include <ctype.h>
#include <limits.h>
#include <stdio.h>

namespace dmAutomationBridge
{
    static bool IsTerminalCommandState(CommandState state)
    {
        return state == COMMAND_COMPLETED || state == COMMAND_FAILED || state == COMMAND_CANCELLED || state == COMMAND_TIMED_OUT;
    }

    static const char* CommandStateName(CommandState state)
    {
        switch (state)
        {
        case COMMAND_PENDING: return "pending";
        case COMMAND_RUNNING: return "running";
        case COMMAND_COMPLETED: return "completed";
        case COMMAND_FAILED: return "failed";
        case COMMAND_CANCELLED: return "cancelled";
        case COMMAND_TIMED_OUT: return "timed_out";
        default: return "unknown";
        }
    }

    static bool IsValidApplicationName(const char* name, bool require_namespace)
    {
        if (IsEmpty(name) || strlen(name) > MAX_APPLICATION_NAME_BYTES || !isalpha((unsigned char)name[0]))
        {
            return false;
        }
        bool has_namespace = false;
        for (const char* cursor = name; *cursor; ++cursor)
        {
            unsigned char c = (unsigned char)*cursor;
            if (c == '.')
            {
                has_namespace = true;
                if (cursor == name || cursor[1] == 0 || cursor[-1] == '.')
                {
                    return false;
                }
            }
            else if (!(isalnum(c) || c == '_' || c == '-' || c == ':'))
            {
                return false;
            }
        }
        return !require_namespace || has_namespace;
    }

    struct JsonCursor
    {
        const char* m_At;
        const char* m_End;
    };

    static void SkipJsonWhitespace(JsonCursor* cursor)
    {
        while (cursor->m_At < cursor->m_End && isspace((unsigned char)*cursor->m_At))
        {
            ++cursor->m_At;
        }
    }

    static bool ParseJsonValue(JsonCursor* cursor, uint32_t depth);

    static bool ParseJsonString(JsonCursor* cursor)
    {
        if (cursor->m_At >= cursor->m_End || *cursor->m_At++ != '"')
        {
            return false;
        }
        while (cursor->m_At < cursor->m_End)
        {
            unsigned char c = (unsigned char)*cursor->m_At++;
            if (c == '"')
            {
                return true;
            }
            if (c < 0x20)
            {
                return false;
            }
            if (c == '\\')
            {
                if (cursor->m_At >= cursor->m_End)
                {
                    return false;
                }
                char escaped = *cursor->m_At++;
                if (strchr("\"\\/bfnrt", escaped))
                {
                    continue;
                }
                if (escaped != 'u' || cursor->m_End - cursor->m_At < 4)
                {
                    return false;
                }
                for (uint32_t i = 0; i < 4; ++i)
                {
                    if (!isxdigit((unsigned char)cursor->m_At[i]))
                    {
                        return false;
                    }
                }
                cursor->m_At += 4;
            }
        }
        return false;
    }

    static bool ParseJsonNumber(JsonCursor* cursor)
    {
        const char* start = cursor->m_At;
        if (cursor->m_At < cursor->m_End && *cursor->m_At == '-') ++cursor->m_At;
        if (cursor->m_At >= cursor->m_End) return false;
        if (*cursor->m_At == '0')
        {
            ++cursor->m_At;
        }
        else
        {
            if (!isdigit((unsigned char)*cursor->m_At) || *cursor->m_At == '0') return false;
            while (cursor->m_At < cursor->m_End && isdigit((unsigned char)*cursor->m_At)) ++cursor->m_At;
        }
        if (cursor->m_At < cursor->m_End && *cursor->m_At == '.')
        {
            ++cursor->m_At;
            if (cursor->m_At >= cursor->m_End || !isdigit((unsigned char)*cursor->m_At)) return false;
            while (cursor->m_At < cursor->m_End && isdigit((unsigned char)*cursor->m_At)) ++cursor->m_At;
        }
        if (cursor->m_At < cursor->m_End && (*cursor->m_At == 'e' || *cursor->m_At == 'E'))
        {
            ++cursor->m_At;
            if (cursor->m_At < cursor->m_End && (*cursor->m_At == '+' || *cursor->m_At == '-')) ++cursor->m_At;
            if (cursor->m_At >= cursor->m_End || !isdigit((unsigned char)*cursor->m_At)) return false;
            while (cursor->m_At < cursor->m_End && isdigit((unsigned char)*cursor->m_At)) ++cursor->m_At;
        }
        return cursor->m_At > start;
    }

    static bool ParseJsonLiteral(JsonCursor* cursor, const char* literal)
    {
        size_t length = strlen(literal);
        if ((size_t)(cursor->m_End - cursor->m_At) < length || strncmp(cursor->m_At, literal, length) != 0)
        {
            return false;
        }
        cursor->m_At += length;
        return true;
    }

    static bool ParseJsonValue(JsonCursor* cursor, uint32_t depth)
    {
        SkipJsonWhitespace(cursor);
        if (cursor->m_At >= cursor->m_End || depth > MAX_APPLICATION_JSON_DEPTH)
        {
            return false;
        }
        if (*cursor->m_At == '"') return ParseJsonString(cursor);
        if (*cursor->m_At == '-' || isdigit((unsigned char)*cursor->m_At)) return ParseJsonNumber(cursor);
        if (*cursor->m_At == 't') return ParseJsonLiteral(cursor, "true");
        if (*cursor->m_At == 'f') return ParseJsonLiteral(cursor, "false");
        if (*cursor->m_At == 'n') return ParseJsonLiteral(cursor, "null");
        char closing = 0;
        bool object = false;
        if (*cursor->m_At == '{') { closing = '}'; object = true; }
        else if (*cursor->m_At == '[') { closing = ']'; }
        else return false;
        ++cursor->m_At;
        SkipJsonWhitespace(cursor);
        if (cursor->m_At < cursor->m_End && *cursor->m_At == closing)
        {
            ++cursor->m_At;
            return true;
        }
        while (cursor->m_At < cursor->m_End)
        {
            if (object)
            {
                if (!ParseJsonString(cursor)) return false;
                SkipJsonWhitespace(cursor);
                if (cursor->m_At >= cursor->m_End || *cursor->m_At++ != ':') return false;
            }
            if (!ParseJsonValue(cursor, depth + 1)) return false;
            SkipJsonWhitespace(cursor);
            if (cursor->m_At >= cursor->m_End) return false;
            char separator = *cursor->m_At++;
            if (separator == closing) return true;
            if (separator != ',') return false;
            SkipJsonWhitespace(cursor);
        }
        return false;
    }

    static bool IsStrictJson(const char* json)
    {
        if (!json) return false;
        size_t length = strlen(json);
        if (length == 0 || length > MAX_APPLICATION_JSON_BYTES) return false;
        JsonCursor cursor = {json, json + length};
        if (!ParseJsonValue(&cursor, 0)) return false;
        SkipJsonWhitespace(&cursor);
        return cursor.m_At == cursor.m_End;
    }

    static void FreeBridgeEvent(BridgeEvent* event)
    {
        FreeString(&event->m_Type);
        FreeString(&event->m_Name);
        FreeString(&event->m_DataJson);
        memset(event, 0, sizeof(*event));
    }

    static void FreePublishedState(PublishedState* state)
    {
        FreeString(&state->m_Name);
        FreeString(&state->m_ValueJson);
        memset(state, 0, sizeof(*state));
    }

    static void FreeCommandInvocation(CommandInvocation* command)
    {
        FreeString(&command->m_Name);
        FreeString(&command->m_ArgumentsJson);
        FreeString(&command->m_ResultJson);
        FreeString(&command->m_Error);
        memset(command, 0, sizeof(*command));
    }

    static void FreeNodeAnnotation(NodeAnnotation* annotation)
    {
        FreeString(&annotation->m_NodeUrl);
        FreeString(&annotation->m_AutomationId);
        FreeString(&annotation->m_LocalizationKey);
        FreeString(&annotation->m_Role);
        memset(annotation, 0, sizeof(*annotation));
    }

    void InitApplicationBridge(uint32_t event_capacity, bool enabled)
    {
        g_AutomationBridge.m_ApplicationApiEnabled = enabled;
        g_AutomationBridge.m_EventCapacity = event_capacity;
        g_AutomationBridge.m_NextEventSequence = 1;
        g_AutomationBridge.m_NextStateRevision = 1;
        g_AutomationBridge.m_NextCommandId = 1;
        g_AutomationBridge.m_ApplicationMutex = dmMutex::New();
        if (IsEmpty(g_AutomationBridge.m_EngineInstanceId))
        {
            dmSnPrintf(g_AutomationBridge.m_EngineInstanceId, sizeof(g_AutomationBridge.m_EngineInstanceId), "engine:%016llx", (unsigned long long)dmTime::GetTime());
        }
    }

    void FinalizeApplicationLua()
    {
        for (uint32_t i = 0; i < g_AutomationBridge.m_CommandHandlers.m_Count; ++i)
        {
            CommandHandler* handler = &g_AutomationBridge.m_CommandHandlers.m_Data[i];
            if (handler->m_Callback)
            {
                dmScript::DestroyCallback(handler->m_Callback);
                handler->m_Callback = 0;
            }
            FreeString(&handler->m_Name);
        }
        ArrayFree(&g_AutomationBridge.m_CommandHandlers);
    }

    void FreeApplicationBridge()
    {
        if (!g_AutomationBridge.m_ApplicationMutex)
        {
            return;
        }
        FinalizeApplicationLua();
        for (uint32_t i = 0; i < g_AutomationBridge.m_Events.m_Count; ++i) FreeBridgeEvent(&g_AutomationBridge.m_Events.m_Data[i]);
        for (uint32_t i = 0; i < g_AutomationBridge.m_PublishedStates.m_Count; ++i) FreePublishedState(&g_AutomationBridge.m_PublishedStates.m_Data[i]);
        for (uint32_t i = 0; i < g_AutomationBridge.m_CommandInvocations.m_Count; ++i) FreeCommandInvocation(&g_AutomationBridge.m_CommandInvocations.m_Data[i]);
        for (uint32_t i = 0; i < g_AutomationBridge.m_NodeAnnotations.m_Count; ++i) FreeNodeAnnotation(&g_AutomationBridge.m_NodeAnnotations.m_Data[i]);
        ArrayFree(&g_AutomationBridge.m_Events);
        ArrayFree(&g_AutomationBridge.m_PublishedStates);
        ArrayFree(&g_AutomationBridge.m_CommandInvocations);
        ArrayFree(&g_AutomationBridge.m_NodeAnnotations);
        dmMutex::Delete(g_AutomationBridge.m_ApplicationMutex);
        g_AutomationBridge.m_ApplicationMutex = 0;
    }

    bool EmitBridgeEvent(const char* type, const char* name, const char* data_json, uint64_t recording_timestamp_us, bool has_recording_timestamp)
    {
        return EmitBridgeEventWithReceipt(type, name, data_json, recording_timestamp_us, has_recording_timestamp, 0, 0);
    }

    bool EmitBridgeEventWithReceipt(const char* type, const char* name, const char* data_json, uint64_t recording_timestamp_us, bool has_recording_timestamp, uint64_t* event_sequence, uint64_t* native_timestamp_us)
    {
        if (!IsValidApplicationName(name, false) || !IsValidApplicationName(type, false) || !IsStrictJson(data_json))
        {
            return false;
        }
        BridgeEvent event;
        memset(&event, 0, sizeof(event));
        event.m_Frame = g_AutomationBridge.m_Frame;
        event.m_SceneSequence = g_AutomationBridge.m_Snapshot.m_Sequence;
        event.m_NativeTimestampUs = dmTime::GetTime();
        event.m_RecordingTimestampUs = recording_timestamp_us;
        event.m_HasRecordingTimestamp = has_recording_timestamp;
        event.m_Type = DuplicateString(type);
        event.m_Name = DuplicateString(name);
        event.m_DataJson = DuplicateString(data_json);
        if (!event.m_Type || !event.m_Name || !event.m_DataJson)
        {
            FreeBridgeEvent(&event);
            return false;
        }

        dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
        event.m_Sequence = g_AutomationBridge.m_NextEventSequence++;
        if (g_AutomationBridge.m_Events.m_Count >= g_AutomationBridge.m_EventCapacity)
        {
            FreeBridgeEvent(&g_AutomationBridge.m_Events.m_Data[0]);
            ArrayErase(&g_AutomationBridge.m_Events, 0);
        }
        if (!ArrayPush(&g_AutomationBridge.m_Events, &event))
        {
            FreeBridgeEvent(&event);
            return false;
        }
        if (event_sequence) *event_sequence = event.m_Sequence;
        if (native_timestamp_us) *native_timestamp_us = event.m_NativeTimestampUs;
        return true;
    }

    uint64_t GetEventNextCursor()
    {
        dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
        return g_AutomationBridge.m_NextEventSequence;
    }

    uint64_t GetEventOldestCursor()
    {
        dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
        return g_AutomationBridge.m_Events.m_Count ? g_AutomationBridge.m_Events.m_Data[0].m_Sequence : g_AutomationBridge.m_NextEventSequence;
    }

    static void AppendBridgeEventJson(StringBuffer* out, const BridgeEvent* event)
    {
        StringBufferAppend(out, "{\"sequence\":");
        AppendNumber(out, (double)event->m_Sequence);
        StringBufferAppend(out, ",\"type\":"); AppendJsonString(out, event->m_Type);
        StringBufferAppend(out, ",\"name\":"); AppendJsonString(out, event->m_Name);
        StringBufferAppend(out, ",\"data\":"); StringBufferAppend(out, event->m_DataJson);
        StringBufferAppend(out, ",\"frame\":"); AppendNumber(out, (double)event->m_Frame);
        StringBufferAppend(out, ",\"native_timestamp_us\":"); AppendNumber(out, (double)event->m_NativeTimestampUs);
        StringBufferAppend(out, ",\"recording_timestamp_us\":");
        if (event->m_HasRecordingTimestamp) AppendNumber(out, (double)event->m_RecordingTimestampUs); else StringBufferAppend(out, "null");
        StringBufferAppend(out, ",\"engine_instance_id\":"); AppendJsonString(out, g_AutomationBridge.m_EngineInstanceId);
        StringBufferAppend(out, ",\"scene_sequence\":"); AppendNumber(out, (double)event->m_SceneSequence);
        StringBufferAppendChar(out, '}');
    }

    uint32_t AppendEventPageJson(StringBuffer* out, uint64_t cursor, uint32_t limit, bool* overflow, uint64_t* next_cursor)
    {
        dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
        uint64_t oldest = g_AutomationBridge.m_Events.m_Count ? g_AutomationBridge.m_Events.m_Data[0].m_Sequence : g_AutomationBridge.m_NextEventSequence;
        *overflow = cursor < oldest;
        uint32_t count = 0;
        *next_cursor = cursor;
        StringBufferAppend(out, "{\"events\":[");
        for (uint32_t i = 0; !*overflow && i < g_AutomationBridge.m_Events.m_Count && count < limit; ++i)
        {
            const BridgeEvent* event = &g_AutomationBridge.m_Events.m_Data[i];
            if (event->m_Sequence < cursor) continue;
            if (count) StringBufferAppendChar(out, ',');
            AppendBridgeEventJson(out, event);
            *next_cursor = event->m_Sequence + 1;
            ++count;
        }
        StringBufferAppend(out, "],\"cursor\":"); AppendNumber(out, (double)cursor);
        StringBufferAppend(out, ",\"next_cursor\":"); AppendNumber(out, (double)*next_cursor);
        StringBufferAppend(out, ",\"oldest_cursor\":"); AppendNumber(out, (double)oldest);
        StringBufferAppend(out, ",\"latest_cursor\":"); AppendNumber(out, (double)g_AutomationBridge.m_NextEventSequence);
        StringBufferAppend(out, ",\"capacity\":"); AppendNumber(out, g_AutomationBridge.m_EventCapacity);
        StringBufferAppend(out, ",\"overflow\":"); StringBufferAppend(out, *overflow ? "true" : "false");
        StringBufferAppendChar(out, '}');
        return count;
    }

    static PublishedState* FindPublishedState(const char* name)
    {
        for (uint32_t i = 0; i < g_AutomationBridge.m_PublishedStates.m_Count; ++i)
        {
            if (StringsEqual(g_AutomationBridge.m_PublishedStates.m_Data[i].m_Name, name)) return &g_AutomationBridge.m_PublishedStates.m_Data[i];
        }
        return 0;
    }

    static bool PublishState(const char* name, const char* value_json)
    {
        if (!IsValidApplicationName(name, false) || !IsStrictJson(value_json)) return false;
        uint64_t revision = 0;
        {
            dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
            PublishedState* state = FindPublishedState(name);
            if (!state)
            {
                PublishedState empty;
                memset(&empty, 0, sizeof(empty));
                empty.m_Name = DuplicateString(name);
                if (!empty.m_Name || !ArrayPush(&g_AutomationBridge.m_PublishedStates, &empty))
                {
                    FreePublishedState(&empty);
                    return false;
                }
                state = &g_AutomationBridge.m_PublishedStates.m_Data[g_AutomationBridge.m_PublishedStates.m_Count - 1];
            }
            if (!SetString(&state->m_ValueJson, value_json)) return false;
            state->m_Revision = g_AutomationBridge.m_NextStateRevision++;
            state->m_Frame = g_AutomationBridge.m_Frame;
            state->m_NativeTimestampUs = dmTime::GetTime();
            revision = state->m_Revision;
        }
        StringBuffer data;
        StringBufferInit(&data);
        StringBufferAppend(&data, "{\"state\":"); AppendJsonString(&data, name);
        StringBufferAppend(&data, ",\"revision\":"); AppendNumber(&data, (double)revision);
        StringBufferAppend(&data, ",\"value\":"); StringBufferAppend(&data, value_json);
        StringBufferAppendChar(&data, '}');
        bool emitted = EmitBridgeEvent("state", "state.published", data.m_Data, 0, false);
        StringBufferFree(&data);
        return emitted;
    }

    uint64_t GetStateRevision()
    {
        dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
        return g_AutomationBridge.m_NextStateRevision - 1;
    }

    uint32_t AppendPublishedStatesJson(StringBuffer* out, const char* name, uint64_t after_revision)
    {
        dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
        uint32_t count = 0;
        StringBufferAppend(out, "{\"states\":[");
        for (uint32_t i = 0; i < g_AutomationBridge.m_PublishedStates.m_Count; ++i)
        {
            const PublishedState* state = &g_AutomationBridge.m_PublishedStates.m_Data[i];
            if (!IsEmpty(name) && !StringsEqual(state->m_Name, name)) continue;
            if (state->m_Revision <= after_revision) continue;
            if (count) StringBufferAppendChar(out, ',');
            StringBufferAppend(out, "{\"name\":"); AppendJsonString(out, state->m_Name);
            StringBufferAppend(out, ",\"value\":"); StringBufferAppend(out, state->m_ValueJson);
            StringBufferAppend(out, ",\"revision\":"); AppendNumber(out, (double)state->m_Revision);
            StringBufferAppend(out, ",\"frame\":"); AppendNumber(out, (double)state->m_Frame);
            StringBufferAppend(out, ",\"native_timestamp_us\":"); AppendNumber(out, (double)state->m_NativeTimestampUs);
            StringBufferAppendChar(out, '}');
            ++count;
        }
        StringBufferAppend(out, "],\"revision\":"); AppendNumber(out, (double)(g_AutomationBridge.m_NextStateRevision - 1));
        StringBufferAppendChar(out, '}');
        return count;
    }

    static CommandHandler* FindCommandHandler(const char* name)
    {
        for (uint32_t i = 0; i < g_AutomationBridge.m_CommandHandlers.m_Count; ++i)
        {
            if (StringsEqual(g_AutomationBridge.m_CommandHandlers.m_Data[i].m_Name, name)) return &g_AutomationBridge.m_CommandHandlers.m_Data[i];
        }
        return 0;
    }

    static CommandInvocation* FindCommandInvocation(uint64_t id)
    {
        for (uint32_t i = 0; i < g_AutomationBridge.m_CommandInvocations.m_Count; ++i)
        {
            if (g_AutomationBridge.m_CommandInvocations.m_Data[i].m_Id == id) return &g_AutomationBridge.m_CommandInvocations.m_Data[i];
        }
        return 0;
    }

    bool SubmitCommand(const char* name, const char* arguments_json, uint32_t timeout_ms, uint64_t* command_id, const char** error)
    {
        if (!g_AutomationBridge.m_ApplicationApiEnabled)
        {
            *error = "application API is not enabled in game.project";
            return false;
        }
        if (!IsValidApplicationName(name, false))
        {
            *error = "command name must be an identifier such as app.load_fixture";
            return false;
        }
        if (!IsStrictJson(arguments_json))
        {
            *error = "command data must be strict JSON, at most 32768 bytes and 16 levels deep";
            return false;
        }
        dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
        if (!FindCommandHandler(name))
        {
            *error = "command is not registered";
            return false;
        }
        if (g_AutomationBridge.m_CommandInvocations.m_Count >= 64)
        {
            uint32_t removable = UINT_MAX;
            for (uint32_t i = 0; i < g_AutomationBridge.m_CommandInvocations.m_Count; ++i)
            {
                if (IsTerminalCommandState(g_AutomationBridge.m_CommandInvocations.m_Data[i].m_State)) { removable = i; break; }
            }
            if (removable == UINT_MAX)
            {
                *error = "command queue is full";
                return false;
            }
            FreeCommandInvocation(&g_AutomationBridge.m_CommandInvocations.m_Data[removable]);
            ArrayErase(&g_AutomationBridge.m_CommandInvocations, removable);
        }
        CommandInvocation command;
        memset(&command, 0, sizeof(command));
        command.m_Id = g_AutomationBridge.m_NextCommandId++;
        command.m_State = COMMAND_PENDING;
        command.m_Name = DuplicateString(name);
        command.m_ArgumentsJson = DuplicateString(arguments_json);
        command.m_AcceptedTimestampUs = dmTime::GetTime();
        command.m_DeadlineTimestampUs = command.m_AcceptedTimestampUs + (uint64_t)timeout_ms * 1000;
        if (!command.m_Name || !command.m_ArgumentsJson || !ArrayPush(&g_AutomationBridge.m_CommandInvocations, &command))
        {
            FreeCommandInvocation(&command);
            *error = "out of memory";
            return false;
        }
        *command_id = command.m_Id;
        return true;
    }

    static void AppendCommandInvocationJson(StringBuffer* out, const CommandInvocation* command)
    {
        StringBufferAppend(out, "{\"command_id\":"); AppendNumber(out, (double)command->m_Id);
        StringBufferAppend(out, ",\"name\":"); AppendJsonString(out, command->m_Name);
        StringBufferAppend(out, ",\"state\":"); AppendJsonString(out, CommandStateName(command->m_State));
        StringBufferAppend(out, ",\"accepted_timestamp_us\":"); AppendNumber(out, (double)command->m_AcceptedTimestampUs);
        StringBufferAppend(out, ",\"completed_timestamp_us\":");
        if (command->m_CompletedTimestampUs) AppendNumber(out, (double)command->m_CompletedTimestampUs); else StringBufferAppend(out, "null");
        StringBufferAppend(out, ",\"result\":");
        if (command->m_ResultJson) StringBufferAppend(out, command->m_ResultJson); else StringBufferAppend(out, "null");
        StringBufferAppend(out, ",\"error\":");
        if (command->m_Error) AppendJsonString(out, command->m_Error); else StringBufferAppend(out, "null");
        StringBufferAppendChar(out, '}');
    }

    bool AppendCommandJson(StringBuffer* out, uint64_t command_id)
    {
        dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
        CommandInvocation* command = FindCommandInvocation(command_id);
        if (!command) return false;
        AppendCommandInvocationJson(out, command);
        return true;
    }

    bool CancelCommand(uint64_t command_id, const char** error)
    {
        dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
        CommandInvocation* command = FindCommandInvocation(command_id);
        if (!command) { *error = "command id was not found"; return false; }
        if (command->m_State == COMMAND_RUNNING) { *error = "running Lua callbacks cannot be preempted"; return false; }
        if (IsTerminalCommandState(command->m_State)) { *error = "command is already complete"; return false; }
        command->m_State = COMMAND_CANCELLED;
        command->m_CompletedTimestampUs = dmTime::GetTime();
        SetString(&command->m_Error, "cancelled before execution");
        return true;
    }

    static bool DecodeJsonProtected(lua_State* L, const char* json)
    {
        lua_getglobal(L, "json");
        if (!lua_istable(L, -1)) { lua_pop(L, 1); return false; }
        lua_getfield(L, -1, "decode");
        lua_remove(L, -2);
        if (!lua_isfunction(L, -1)) { lua_pop(L, 1); return false; }
        lua_pushlstring(L, json, strlen(json));
        return dmScript::PCall(L, 1, 1) == 0;
    }

    static bool EncodeLuaJson(lua_State* L, int index, char** json, const char** error)
    {
        int absolute_index = index < 0 ? lua_gettop(L) + index + 1 : index;
        lua_getglobal(L, "json");
        if (!lua_istable(L, -1))
        {
            lua_pop(L, 1);
            *error = "Defold json module is unavailable";
            return false;
        }
        lua_getfield(L, -1, "encode");
        lua_remove(L, -2);
        if (!lua_isfunction(L, -1))
        {
            lua_pop(L, 1);
            *error = "Defold json.encode is unavailable";
            return false;
        }
        lua_pushvalue(L, absolute_index);
        if (dmScript::PCall(L, 1, 1) != 0)
        {
            *error = "value is not JSON encodable";
            return false;
        }
        size_t json_length = 0;
        const char* encoded = lua_tolstring(L, -1, &json_length);
        if (!encoded || json_length > MAX_APPLICATION_JSON_BYTES)
        {
            lua_pop(L, 1);
            *error = "JSON value exceeds the 32768 byte or 16-level limit";
            return false;
        }
        *json = DuplicateStringN(encoded, (uint32_t)json_length);
        lua_pop(L, 1);
        if (!*json || !IsStrictJson(*json))
        {
            free(*json);
            *json = 0;
            *error = "JSON value exceeds the 32768 byte or 16-level limit";
            return false;
        }
        return true;
    }

    static void CompleteCommand(uint64_t id, CommandState state, const char* result_json, const char* error)
    {
        char* command_name = 0;
        CommandState final_state = state;
        {
            dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
            CommandInvocation* command = FindCommandInvocation(id);
            if (!command) return;
            uint64_t now = dmTime::GetTime();
            if (command->m_DeadlineTimestampUs <= now && (state == COMMAND_COMPLETED || state == COMMAND_FAILED))
            {
                final_state = COMMAND_TIMED_OUT;
                result_json = 0;
                error = "Lua callback exceeded its deadline; execution could not be preempted";
            }
            command->m_State = final_state;
            command->m_CompletedTimestampUs = now;
            if (result_json) SetString(&command->m_ResultJson, result_json);
            if (error) SetString(&command->m_Error, error);
            command_name = DuplicateString(command->m_Name);
        }
        if (command_name)
        {
            StringBuffer data;
            StringBufferInit(&data);
            StringBufferAppend(&data, "{\"command_id\":"); AppendNumber(&data, (double)id);
            StringBufferAppend(&data, ",\"name\":"); AppendJsonString(&data, command_name);
            StringBufferAppend(&data, ",\"state\":"); AppendJsonString(&data, CommandStateName(final_state));
            StringBufferAppendChar(&data, '}');
            EmitBridgeEvent("command", "command.finished", data.m_Data, 0, false);
            StringBufferFree(&data);
            free(command_name);
        }
    }

    void UpdateApplicationBridge()
    {
        uint64_t id = 0;
        char* arguments_json = 0;
        dmScript::LuaCallbackInfo* callback = 0;
        {
            dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
            uint64_t now = dmTime::GetTime();
            for (uint32_t i = 0; i < g_AutomationBridge.m_CommandInvocations.m_Count; ++i)
            {
                CommandInvocation* command = &g_AutomationBridge.m_CommandInvocations.m_Data[i];
                if (command->m_State != COMMAND_PENDING) continue;
                if (command->m_DeadlineTimestampUs <= now)
                {
                    command->m_State = COMMAND_TIMED_OUT;
                    command->m_CompletedTimestampUs = now;
                    SetString(&command->m_Error, "timed out before execution");
                    continue;
                }
                CommandHandler* handler = FindCommandHandler(command->m_Name);
                if (!handler || !handler->m_Callback || !dmScript::IsCallbackValid(handler->m_Callback))
                {
                    command->m_State = COMMAND_FAILED;
                    command->m_CompletedTimestampUs = now;
                    SetString(&command->m_Error, "registered command callback is no longer valid");
                    continue;
                }
                command->m_State = COMMAND_RUNNING;
                id = command->m_Id;
                arguments_json = DuplicateString(command->m_ArgumentsJson);
                callback = handler->m_Callback;
                break;
            }
        }
        if (!id || !arguments_json || !callback)
        {
            free(arguments_json);
            return;
        }

        lua_State* L = dmScript::GetCallbackLuaContext(callback);
        int top = lua_gettop(L);
        if (!DecodeJsonProtected(L, arguments_json))
        {
            lua_settop(L, top);
            CompleteCommand(id, COMMAND_FAILED, 0, "command arguments failed strict JSON decoding");
            free(arguments_json);
            return;
        }
        int argument_ref = luaL_ref(L, LUA_REGISTRYINDEX);
        g_AutomationBridge.m_CommandExecuting = true;
        if (!dmScript::SetupCallback(callback))
        {
            luaL_unref(L, LUA_REGISTRYINDEX, argument_ref);
            g_AutomationBridge.m_CommandExecuting = false;
            CompleteCommand(id, COMMAND_FAILED, 0, "command callback instance is unavailable");
            free(arguments_json);
            return;
        }
        // SetupCallback supplies the owning script instance for context, but the
        // public command API deliberately calls handlers as function(data).
        lua_pop(L, 1);
        lua_rawgeti(L, LUA_REGISTRYINDEX, argument_ref);
        luaL_unref(L, LUA_REGISTRYINDEX, argument_ref);
        int call_result = dmScript::PCall(L, 1, 1);
        char* result_json = 0;
        const char* encode_error = 0;
        if (call_result == 0)
        {
            if (lua_isnil(L, -1)) result_json = DuplicateString("null");
            else EncodeLuaJson(L, -1, &result_json, &encode_error);
            lua_pop(L, 1);
        }
        dmScript::TeardownCallback(callback);
        g_AutomationBridge.m_CommandExecuting = false;
        if (call_result != 0) CompleteCommand(id, COMMAND_FAILED, 0, "Lua command callback raised an error");
        else if (!result_json) CompleteCommand(id, COMMAND_FAILED, 0, encode_error ? encode_error : "command result was not JSON encodable");
        else CompleteCommand(id, COMMAND_COMPLETED, result_json, 0);
        free(result_json);
        free(arguments_json);
    }

    bool AddTimelineMarker(const char* name, const char* data_json, uint64_t recording_timestamp_us, bool has_recording_timestamp, uint64_t* event_sequence, uint64_t* native_timestamp_us)
    {
        return EmitBridgeEventWithReceipt("marker", name, data_json, recording_timestamp_us, has_recording_timestamp, event_sequence, native_timestamp_us);
    }

    void ApplyNodeAnnotation(Node* node)
    {
        if (IsEmpty(node->m_Url) || !g_AutomationBridge.m_ApplicationMutex) return;
        dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
        for (uint32_t i = 0; i < g_AutomationBridge.m_NodeAnnotations.m_Count; ++i)
        {
            const NodeAnnotation* annotation = &g_AutomationBridge.m_NodeAnnotations.m_Data[i];
            if (!StringsEqual(node->m_Url, annotation->m_NodeUrl)) continue;
            SetString(&node->m_AutomationId, annotation->m_AutomationId);
            SetString(&node->m_LocalizationKey, annotation->m_LocalizationKey);
            SetString(&node->m_Role, annotation->m_Role);
            return;
        }
    }

    static int LuaEmit(lua_State* L)
    {
        DM_LUA_STACK_CHECK(L, 0);
        const char* name = luaL_checkstring(L, 1);
        if (!IsValidApplicationName(name, false)) return luaL_error(L, "event name must be a valid identifier of at most %u bytes", MAX_APPLICATION_NAME_BYTES);
        bool pushed_default = lua_gettop(L) < 2;
        if (pushed_default) lua_newtable(L);
        char* json = 0;
        const char* error = 0;
        if (!EncodeLuaJson(L, 2, &json, &error))
        {
            if (pushed_default) lua_pop(L, 1);
            return luaL_error(L, "%s", error);
        }
        bool ok = EmitBridgeEvent("event", name, json, 0, false);
        free(json);
        if (pushed_default) lua_pop(L, 1);
        if (!ok) return luaL_error(L, "failed to retain event");
        return 0;
    }

    static int LuaPublish(lua_State* L)
    {
        DM_LUA_STACK_CHECK(L, 0);
        const char* name = luaL_checkstring(L, 1);
        if (!IsValidApplicationName(name, false)) return luaL_error(L, "state name must be an identifier, preferably namespaced such as app.ui");
        char* json = 0;
        const char* error = 0;
        if (!EncodeLuaJson(L, 2, &json, &error)) return luaL_error(L, "%s", error);
        bool ok = PublishState(name, json);
        free(json);
        if (!ok) return luaL_error(L, "failed to publish state");
        return 0;
    }

    static int LuaCommand(lua_State* L)
    {
        DM_LUA_STACK_CHECK(L, 0);
        if (g_AutomationBridge.m_CommandExecuting) return luaL_error(L, "commands cannot be registered while a command callback is executing");
        const char* name = luaL_checkstring(L, 1);
        luaL_checktype(L, 2, LUA_TFUNCTION);
        if (!IsValidApplicationName(name, false)) return luaL_error(L, "command name must be an identifier, preferably namespaced such as app.load_fixture");
        dmScript::LuaCallbackInfo* callback = dmScript::CreateCallback(L, 2);
        if (!callback) return luaL_error(L, "command callback must be registered from a script instance");
        dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
        CommandHandler* handler = FindCommandHandler(name);
        if (handler)
        {
            dmScript::DestroyCallback(handler->m_Callback);
            handler->m_Callback = callback;
            return 0;
        }
        CommandHandler new_handler;
        new_handler.m_Name = DuplicateString(name);
        new_handler.m_Callback = callback;
        if (!new_handler.m_Name || !ArrayPush(&g_AutomationBridge.m_CommandHandlers, &new_handler))
        {
            free(new_handler.m_Name);
            dmScript::DestroyCallback(callback);
            return luaL_error(L, "failed to retain command callback");
        }
        return 0;
    }

    static int LuaAcknowledgeInput(lua_State* L)
    {
        DM_LUA_STACK_CHECK(L, 0);
        lua_Number input_number = luaL_checknumber(L, 1);
        if (input_number < 1 || input_number > 9007199254740991.0 || floor(input_number) != input_number) return luaL_error(L, "input id must be a positive integer");
        bool pushed_default = lua_gettop(L) < 2;
        if (pushed_default) lua_newtable(L);
        char* result_json = 0;
        const char* error = 0;
        if (!EncodeLuaJson(L, 2, &result_json, &error))
        {
            if (pushed_default) lua_pop(L, 1);
            return luaL_error(L, "%s", error);
        }
        StringBuffer data;
        StringBufferInit(&data);
        StringBufferAppend(&data, "{\"input_id\":"); AppendNumber(&data, (double)input_number);
        StringBufferAppend(&data, ",\"result\":"); StringBufferAppend(&data, result_json);
        StringBufferAppendChar(&data, '}');
        bool ok = EmitBridgeEvent("acknowledgement", "input.acknowledged", data.m_Data, 0, false);
        StringBufferFree(&data);
        free(result_json);
        if (pushed_default) lua_pop(L, 1);
        if (!ok) return luaL_error(L, "failed to retain acknowledgement");
        return 0;
    }

    static char* OptionalTableString(lua_State* L, int index, const char* field)
    {
        lua_getfield(L, index, field);
        char* result = 0;
        if (!lua_isnil(L, -1)) result = DuplicateString(luaL_checkstring(L, -1));
        lua_pop(L, 1);
        return result;
    }

    static int LuaAnnotate(lua_State* L)
    {
        DM_LUA_STACK_CHECK(L, 0);
        const char* node_url = luaL_checkstring(L, 1);
        luaL_checktype(L, 2, LUA_TTABLE);
        NodeAnnotation annotation;
        memset(&annotation, 0, sizeof(annotation));
        annotation.m_NodeUrl = DuplicateString(node_url);
        annotation.m_AutomationId = OptionalTableString(L, 2, "automation_id");
        annotation.m_LocalizationKey = OptionalTableString(L, 2, "localization_key");
        annotation.m_Role = OptionalTableString(L, 2, "role");
        if (!annotation.m_NodeUrl || (IsEmpty(annotation.m_AutomationId) && IsEmpty(annotation.m_LocalizationKey) && IsEmpty(annotation.m_Role)))
        {
            FreeNodeAnnotation(&annotation);
            return luaL_error(L, "annotation requires automation_id, localization_key, or role");
        }
        dmMutex::ScopedLock lock(g_AutomationBridge.m_ApplicationMutex);
        for (uint32_t i = 0; i < g_AutomationBridge.m_NodeAnnotations.m_Count; ++i)
        {
            NodeAnnotation* existing = &g_AutomationBridge.m_NodeAnnotations.m_Data[i];
            if (!StringsEqual(existing->m_NodeUrl, node_url)) continue;
            FreeNodeAnnotation(existing);
            *existing = annotation;
            g_AutomationBridge.m_SnapshotFrame = UINT64_MAX;
            return 0;
        }
        if (!ArrayPush(&g_AutomationBridge.m_NodeAnnotations, &annotation))
        {
            FreeNodeAnnotation(&annotation);
            return luaL_error(L, "failed to retain annotation");
        }
        g_AutomationBridge.m_SnapshotFrame = UINT64_MAX;
        return 0;
    }

    void RegisterApplicationLua(lua_State* L)
    {
        if (!g_AutomationBridge.m_ApplicationApiEnabled || !L) return;
        static const luaL_reg methods[] = {
            {"emit", LuaEmit},
            {"publish", LuaPublish},
            {"command", LuaCommand},
            {"acknowledge_input", LuaAcknowledgeInput},
            {"annotate", LuaAnnotate},
            {0, 0}
        };
        luaL_register(L, "automation_bridge", methods);
        lua_pop(L, 1);
    }
}

#endif
