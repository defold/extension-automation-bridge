#include "automation_bridge_private.h"

#if defined(DM_DEBUG)

#include <dmsdk/dlib/utf8.h>

#include <stdint.h>
#include <string.h>

namespace dmAutomationBridge
{
    static const float INPUT_VISUALIZATION_SECONDS = 1.0f;
    static const float DEFAULT_CONTROLLER_LEASE_SECONDS = 5.0f;

    static const char* NormalizedId(const char* value, const char* fallback)
    {
        return IsEmpty(value) ? fallback : value;
    }

    const char* InputStateName(InputState state)
    {
        switch (state)
        {
            case INPUT_STATE_ACCEPTED: return "accepted";
            case INPUT_STATE_STARTED: return "started";
            case INPUT_STATE_RELEASED: return "released";
            case INPUT_STATE_CANCELLED: return "cancelled";
            case INPUT_STATE_FAILED: return "failed";
        }
        return "failed";
    }

    const char* InputDeviceName(InputDevice device)
    {
        switch (device)
        {
            case INPUT_DEVICE_AUTO: return "auto";
            case INPUT_DEVICE_MOUSE: return "mouse";
            case INPUT_DEVICE_TOUCH: return "touch";
        }
        return "auto";
    }

    bool ParseInputDevice(const char* value, InputDevice* device)
    {
        if (IsEmpty(value) || StringsEqual(value, "auto"))
        {
            *device = INPUT_DEVICE_AUTO;
            return true;
        }
        if (StringsEqual(value, "mouse"))
        {
            *device = INPUT_DEVICE_MOUSE;
            return true;
        }
        if (StringsEqual(value, "touch"))
        {
            *device = INPUT_DEVICE_TOUCH;
            return true;
        }
        return false;
    }

    bool ParseInputEasing(const char* value, InputEasing* easing)
    {
        if (IsEmpty(value) || StringsEqual(value, "linear"))
        {
            *easing = INPUT_EASING_LINEAR;
            return true;
        }
        if (StringsEqual(value, "ease_in"))
        {
            *easing = INPUT_EASING_EASE_IN;
            return true;
        }
        if (StringsEqual(value, "ease_out"))
        {
            *easing = INPUT_EASING_EASE_OUT;
            return true;
        }
        if (StringsEqual(value, "ease_in_out"))
        {
            *easing = INPUT_EASING_EASE_IN_OUT;
            return true;
        }
        return false;
    }

    bool IsInputDeviceSupported(InputDevice device)
    {
        if (device == INPUT_DEVICE_AUTO || device == INPUT_DEVICE_MOUSE)
        {
            return true;
        }
#if defined(DM_PLATFORM_IOS) || defined(DM_PLATFORM_ANDROID) || defined(DM_PLATFORM_SWITCH)
        return device == INPUT_DEVICE_TOUCH;
#else
        return false;
#endif
    }

    InputDevice ResolveInputDevice(InputDevice device)
    {
        if (device != INPUT_DEVICE_AUTO)
        {
            return device;
        }
#if defined(DM_PLATFORM_IOS) || defined(DM_PLATFORM_ANDROID) || defined(DM_PLATFORM_SWITCH)
        return INPUT_DEVICE_TOUCH;
#else
        return INPUT_DEVICE_MOUSE;
#endif
    }

    void FreeInputReceipt(InputReceipt* receipt)
    {
        if (!receipt)
        {
            return;
        }
        FreeString(&receipt->m_Kind);
        FreeString(&receipt->m_Reason);
        FreeString(&receipt->m_ClientId);
        FreeString(&receipt->m_SessionId);
        FreeString(&receipt->m_RequestId);
        memset(receipt, 0, sizeof(*receipt));
    }

    void FreeInputEvent(InputEvent* event)
    {
        if (!event)
        {
            return;
        }
        FreeInputReceipt(&event->m_Receipt);
        ArrayFree(&event->m_Points);
        FreeString(&event->m_Keys);
        memset(event, 0, sizeof(*event));
    }

    static bool ReceiptSetReason(InputReceipt* receipt, const char* reason)
    {
        return SetString(&receipt->m_Reason, IsEmpty(reason) ? "" : reason);
    }

    static bool InitReceipt(InputReceipt* receipt, const char* kind, const char* client_id, const char* session_id,
                            const char* request_id, uint64_t scene_sequence, InputDevice device, uint32_t pointer_id)
    {
        memset(receipt, 0, sizeof(*receipt));
        receipt->m_Id = g_AutomationBridge.m_NextInputId++;
        receipt->m_State = INPUT_STATE_ACCEPTED;
        receipt->m_AcceptedFrame = g_AutomationBridge.m_Frame;
        receipt->m_AcceptedTime = dmTime::GetTime();
        receipt->m_SceneSequence = scene_sequence;
        receipt->m_Device = ResolveInputDevice(device);
        receipt->m_PointerId = pointer_id;
        return SetString(&receipt->m_Kind, kind) &&
               SetString(&receipt->m_ClientId, NormalizedId(client_id, "anonymous")) &&
               SetString(&receipt->m_SessionId, NormalizedId(session_id, "default")) &&
               SetString(&receipt->m_RequestId, NormalizedId(request_id, ""));
    }

    static bool CanQueueInputEvent()
    {
        return g_AutomationBridge.m_InputEvents.m_Count < MAX_INPUT_EVENTS;
    }

    static void MoveReceiptToHistory(InputEvent* event)
    {
        if (g_AutomationBridge.m_InputHistory.m_Count >= MAX_INPUT_HISTORY)
        {
            FreeInputReceipt(&g_AutomationBridge.m_InputHistory.m_Data[0]);
            ArrayErase(&g_AutomationBridge.m_InputHistory, 0);
        }
        if (ArrayPush(&g_AutomationBridge.m_InputHistory, &event->m_Receipt))
        {
            memset(&event->m_Receipt, 0, sizeof(event->m_Receipt));
        }
    }

    static void RemoveFinishedEvent(uint32_t index)
    {
        InputEvent* event = &g_AutomationBridge.m_InputEvents.m_Data[index];
        MoveReceiptToHistory(event);
        FreeInputEvent(event);
        ArrayErase(&g_AutomationBridge.m_InputEvents, index);
    }

    bool IsInputController(const char* client_id, const char* session_id)
    {
        return StringsEqual(g_AutomationBridge.m_ControllerClientId, NormalizedId(client_id, "anonymous")) &&
               StringsEqual(g_AutomationBridge.m_ControllerSessionId, NormalizedId(session_id, "default"));
    }

    bool AcquireInputController(const char* client_id, const char* session_id, float lease, const char** error)
    {
        uint64_t now = dmTime::GetTime();
        const char* normalized_client = NormalizedId(client_id, "anonymous");
        const char* normalized_session = NormalizedId(session_id, "default");
        if (!IsEmpty(g_AutomationBridge.m_ControllerClientId) && g_AutomationBridge.m_ControllerLeaseDeadline <= now)
        {
            FlushInput(g_AutomationBridge.m_ControllerClientId, g_AutomationBridge.m_ControllerSessionId, true, "controller_lease_expired");
            FreeString(&g_AutomationBridge.m_ControllerClientId);
            FreeString(&g_AutomationBridge.m_ControllerSessionId);
        }
        if (!IsEmpty(g_AutomationBridge.m_ControllerClientId) && !IsInputController(normalized_client, normalized_session))
        {
            *error = "another client/session holds the input controller lease";
            return false;
        }
        if (!SetString(&g_AutomationBridge.m_ControllerClientId, normalized_client) ||
            !SetString(&g_AutomationBridge.m_ControllerSessionId, normalized_session))
        {
            *error = "unable to store input controller identity";
            return false;
        }
        float lease_seconds = lease > 0.0f ? lease : DEFAULT_CONTROLLER_LEASE_SECONDS;
        lease_seconds = ClampFloat(lease_seconds, 0.1f, MAX_INPUT_DURATION);
        g_AutomationBridge.m_ControllerLeaseDeadline = now + (uint64_t)(lease_seconds * 1000000.0f);
        return true;
    }

    void ReleaseInputController(const char* client_id, const char* session_id)
    {
        if (!IsInputController(client_id, session_id))
        {
            return;
        }
        FreeString(&g_AutomationBridge.m_ControllerClientId);
        FreeString(&g_AutomationBridge.m_ControllerSessionId);
        g_AutomationBridge.m_ControllerLeaseDeadline = 0;
    }

    static float RequestedPathDuration(const Array<InputPoint>* points, InputPathMode path_mode)
    {
        float total = 0.0f;
        if (path_mode == INPUT_PATH_SAMPLED)
        {
            for (uint32_t i = 1; i < points->m_Count; ++i)
            {
                total += points->m_Data[i].m_Duration;
            }
        }
        else if (points->m_Count > 1)
        {
            total = points->m_Data[points->m_Count - 1].m_Duration;
        }
        return total;
    }

    bool AddMouseInput(const Array<InputPoint>* points, InputPathMode path_mode, float hold_before, float hold_after,
                       InputDevice device, uint32_t pointer_id, bool visualize, const char* kind,
                       const char* client_id, const char* session_id, const char* request_id,
                       uint64_t scene_sequence, float lease, bool pointer_open, InputReceipt** receipt)
    {
        if (!points || points->m_Count == 0 || points->m_Count > MAX_INPUT_PATH_POINTS || !CanQueueInputEvent())
        {
            return false;
        }
        InputDevice resolved = ResolveInputDevice(device);
        if (!IsInputDeviceSupported(resolved))
        {
            return false;
        }

        InputEvent event;
        memset(&event, 0, sizeof(event));
        event.m_Type = pointer_open ? INPUT_EVENT_POINTER : INPUT_EVENT_MOUSE;
        event.m_PathMode = path_mode;
        event.m_HoldBefore = MaxFloat(0.0f, hold_before);
        event.m_HoldAfter = MaxFloat(0.0f, hold_after);
        event.m_MouseButton = dmHID::MOUSE_BUTTON_LEFT;
        event.m_Visualize = visualize;
        event.m_ActiveKey = dmHID::MAX_KEY_COUNT;
        event.m_PointerOpen = pointer_open;
        event.m_LeaseDeadline = pointer_open ? dmTime::GetTime() + (uint64_t)(ClampFloat(lease, 0.1f, MAX_INPUT_DURATION) * 1000000.0f) : 0;
        if (!InitReceipt(&event.m_Receipt, kind, client_id, session_id, request_id, scene_sequence, resolved, pointer_id) ||
            !ArraySetCount(&event.m_Points, points->m_Count))
        {
            FreeInputEvent(&event);
            return false;
        }
        memcpy(event.m_Points.m_Data, points->m_Data, sizeof(InputPoint) * points->m_Count);
        event.m_Receipt.m_RequestedDuration = event.m_HoldBefore + RequestedPathDuration(points, path_mode) + event.m_HoldAfter;
        if (!ArrayPush(&g_AutomationBridge.m_InputEvents, &event))
        {
            FreeInputEvent(&event);
            return false;
        }
        *receipt = &g_AutomationBridge.m_InputEvents.m_Data[g_AutomationBridge.m_InputEvents.m_Count - 1].m_Receipt;
        return true;
    }

    bool AddKeyInput(const char* keys, const char* client_id, const char* session_id, const char* request_id,
                     uint64_t scene_sequence, InputReceipt** receipt)
    {
        if (IsEmpty(keys) || !CanQueueInputEvent())
        {
            return false;
        }
        InputEvent event;
        memset(&event, 0, sizeof(event));
        event.m_Type = INPUT_EVENT_KEYS;
        event.m_Keys = DuplicateString(keys);
        event.m_ActiveKey = dmHID::MAX_KEY_COUNT;
        if (!event.m_Keys || !InitReceipt(&event.m_Receipt, "key", client_id, session_id, request_id,
                                          scene_sequence, INPUT_DEVICE_AUTO, 0) ||
            !ArrayPush(&g_AutomationBridge.m_InputEvents, &event))
        {
            FreeInputEvent(&event);
            return false;
        }
        *receipt = &g_AutomationBridge.m_InputEvents.m_Data[g_AutomationBridge.m_InputEvents.m_Count - 1].m_Receipt;
        return true;
    }

    static InputEvent* FindQueuedEvent(uint64_t input_id, uint32_t* index)
    {
        for (uint32_t i = 0; i < g_AutomationBridge.m_InputEvents.m_Count; ++i)
        {
            if (g_AutomationBridge.m_InputEvents.m_Data[i].m_Receipt.m_Id == input_id)
            {
                if (index) *index = i;
                return &g_AutomationBridge.m_InputEvents.m_Data[i];
            }
        }
        return 0;
    }

    const InputReceipt* FindInputReceipt(uint64_t input_id)
    {
        InputEvent* event = FindQueuedEvent(input_id, 0);
        if (event)
        {
            return &event->m_Receipt;
        }
        for (uint32_t i = 0; i < g_AutomationBridge.m_InputHistory.m_Count; ++i)
        {
            if (g_AutomationBridge.m_InputHistory.m_Data[i].m_Id == input_id)
            {
                return &g_AutomationBridge.m_InputHistory.m_Data[i];
            }
        }
        return 0;
    }

    static void RefreshPointerLease(InputEvent* event, float lease)
    {
        event->m_LeaseDeadline = dmTime::GetTime() + (uint64_t)(ClampFloat(lease, 0.1f, MAX_INPUT_DURATION) * 1000000.0f);
    }

    bool AppendPointerMove(uint64_t input_id, const InputPoint* point, float lease, const char** error)
    {
        InputEvent* event = FindQueuedEvent(input_id, 0);
        if (!event || !event->m_PointerOpen)
        {
            *error = "pointer session was not found or is already closed";
            return false;
        }
        if (!point || event->m_Points.m_Count >= MAX_INPUT_PATH_POINTS)
        {
            *error = "pointer path has reached the maximum point count";
            return false;
        }
        uint32_t previous_count = event->m_Points.m_Count;
        if (!ArrayPush(&event->m_Points, point))
        {
            *error = "unable to append pointer path point";
            return false;
        }
        event->m_Receipt.m_RequestedDuration += point->m_Duration;
        if (event->m_Phase == 4)
        {
            event->m_Segment = previous_count - 1;
            event->m_Elapsed = 0.0f;
            event->m_Phase = 2;
        }
        RefreshPointerLease(event, lease);
        return true;
    }

    bool AppendPointerHold(uint64_t input_id, float duration, float lease, const char** error)
    {
        InputEvent* event = FindQueuedEvent(input_id, 0);
        if (!event || event->m_Points.m_Count == 0)
        {
            *error = "pointer session was not found or is already closed";
            return false;
        }
        InputPoint point = event->m_Points.m_Data[event->m_Points.m_Count - 1];
        point.m_Duration = duration;
        point.m_Easing = INPUT_EASING_LINEAR;
        return AppendPointerMove(input_id, &point, lease, error);
    }

    bool ReleasePointer(uint64_t input_id, const char** error)
    {
        InputEvent* event = FindQueuedEvent(input_id, 0);
        if (!event || !event->m_PointerOpen)
        {
            *error = "pointer session was not found or is already closed";
            return false;
        }
        event->m_PointerOpen = false;
        event->m_ReleaseRequested = true;
        return true;
    }

    static void AppendUInt64(StringBuffer* out, uint64_t value)
    {
        char buffer[32];
        dmSnPrintf(buffer, sizeof(buffer), "%llu", (unsigned long long)value);
        StringBufferAppend(out, buffer);
    }

    static void AppendOptionalFrame(StringBuffer* out, uint64_t value)
    {
        if (value == 0)
        {
            StringBufferAppend(out, "null");
        }
        else
        {
            AppendUInt64(out, value);
        }
    }

    void AppendInputReceiptJson(StringBuffer* out, const InputReceipt* receipt, uint32_t queue_position)
    {
        StringBufferAppend(out, "{\"input_id\":");
        AppendUInt64(out, receipt->m_Id);
        StringBufferAppend(out, ",\"kind\":");
        AppendJsonString(out, receipt->m_Kind);
        StringBufferAppend(out, ",\"state\":");
        AppendJsonString(out, InputStateName(receipt->m_State));
        StringBufferAppend(out, ",\"queue_position\":");
        AppendNumber(out, (double)queue_position);
        StringBufferAppend(out, ",\"accepted_frame\":");
        AppendOptionalFrame(out, receipt->m_AcceptedFrame);
        StringBufferAppend(out, ",\"start_frame\":");
        AppendOptionalFrame(out, receipt->m_StartFrame);
        StringBufferAppend(out, ",\"release_frame\":");
        AppendOptionalFrame(out, receipt->m_ReleaseFrame);
        StringBufferAppend(out, ",\"accepted_time_us\":");
        AppendUInt64(out, receipt->m_AcceptedTime);
        StringBufferAppend(out, ",\"start_time_us\":");
        AppendOptionalFrame(out, receipt->m_StartTime);
        StringBufferAppend(out, ",\"release_time_us\":");
        AppendOptionalFrame(out, receipt->m_ReleaseTime);
        StringBufferAppend(out, ",\"requested_duration\":");
        AppendNumber(out, receipt->m_RequestedDuration);
        StringBufferAppend(out, ",\"actual_duration\":");
        AppendNumber(out, receipt->m_StartTime && receipt->m_ReleaseTime ? (double)(receipt->m_ReleaseTime - receipt->m_StartTime) / 1000000.0 : 0.0);
        StringBufferAppend(out, ",\"reason\":");
        if (IsEmpty(receipt->m_Reason)) StringBufferAppend(out, "null"); else AppendJsonString(out, receipt->m_Reason);
        StringBufferAppend(out, ",\"client_id\":");
        AppendJsonString(out, receipt->m_ClientId);
        StringBufferAppend(out, ",\"session_id\":");
        AppendJsonString(out, receipt->m_SessionId);
        StringBufferAppend(out, ",\"request_id\":");
        AppendJsonString(out, receipt->m_RequestId);
        StringBufferAppend(out, ",\"engine_instance_id\":");
        AppendJsonString(out, g_AutomationBridge.m_EngineInstanceId);
        StringBufferAppend(out, ",\"scene_sequence\":");
        AppendUInt64(out, receipt->m_SceneSequence);
        StringBufferAppend(out, ",\"engine_frame\":");
        AppendUInt64(out, g_AutomationBridge.m_Frame);
        StringBufferAppend(out, ",\"device\":");
        AppendJsonString(out, InputDeviceName(receipt->m_Device));
        StringBufferAppend(out, ",\"pointer_id\":");
        AppendNumber(out, (double)receipt->m_PointerId);
        StringBufferAppendChar(out, '}');
    }

    bool GetNodeCenter(const char* id, float* x, float* y, const char** error)
    {
        const Node* node = FindNodeById(id);
        if (!node)
        {
            *error = "node id was not found";
            return false;
        }
        if (!node->m_Bounds.m_Valid)
        {
            *error = "node has no screen bounds";
            return false;
        }
        *x = node->m_Bounds.m_CX;
        *y = node->m_Bounds.m_CY;
        return true;
    }

    static dmHID::Key KeyFromName(const char* name)
    {
        struct KeyMap { const char* m_Name; dmHID::Key m_Key; };
        static const KeyMap keys[] = {
            {"KEY_SPACE", dmHID::KEY_SPACE}, {"KEY_ESC", dmHID::KEY_ESC}, {"KEY_ESCAPE", dmHID::KEY_ESC},
            {"KEY_UP", dmHID::KEY_UP}, {"KEY_DOWN", dmHID::KEY_DOWN}, {"KEY_LEFT", dmHID::KEY_LEFT},
            {"KEY_RIGHT", dmHID::KEY_RIGHT}, {"KEY_TAB", dmHID::KEY_TAB}, {"KEY_ENTER", dmHID::KEY_ENTER},
            {"KEY_BACKSPACE", dmHID::KEY_BACKSPACE}, {"KEY_INSERT", dmHID::KEY_INSERT}, {"KEY_DEL", dmHID::KEY_DEL},
            {"KEY_DELETE", dmHID::KEY_DEL}, {"KEY_PAGEUP", dmHID::KEY_PAGEUP}, {"KEY_PAGEDOWN", dmHID::KEY_PAGEDOWN},
            {"KEY_HOME", dmHID::KEY_HOME}, {"KEY_END", dmHID::KEY_END}, {"KEY_LSHIFT", dmHID::KEY_LSHIFT},
            {"KEY_RSHIFT", dmHID::KEY_RSHIFT}, {"KEY_LCTRL", dmHID::KEY_LCTRL}, {"KEY_RCTRL", dmHID::KEY_RCTRL},
            {"KEY_LALT", dmHID::KEY_LALT}, {"KEY_RALT", dmHID::KEY_RALT},
            {"KEY_F1", dmHID::KEY_F1}, {"KEY_F2", dmHID::KEY_F2}, {"KEY_F3", dmHID::KEY_F3},
            {"KEY_F4", dmHID::KEY_F4}, {"KEY_F5", dmHID::KEY_F5}, {"KEY_F6", dmHID::KEY_F6},
            {"KEY_F7", dmHID::KEY_F7}, {"KEY_F8", dmHID::KEY_F8}, {"KEY_F9", dmHID::KEY_F9},
            {"KEY_F10", dmHID::KEY_F10}, {"KEY_F11", dmHID::KEY_F11}, {"KEY_F12", dmHID::KEY_F12}
        };
        for (uint32_t i = 0; i < DM_ARRAY_SIZE(keys); ++i)
        {
            if (StringsEqual(name, keys[i].m_Name)) return keys[i].m_Key;
        }
        if (strlen(name) == 5 && StartsWith(name, "KEY_") && name[4] >= 'A' && name[4] <= 'Z')
            return (dmHID::Key)(dmHID::KEY_A + (name[4] - 'A'));
        if (strlen(name) == 5 && StartsWith(name, "KEY_") && name[4] >= '0' && name[4] <= '9')
            return (dmHID::Key)(dmHID::KEY_0 + (name[4] - '0'));
        return dmHID::MAX_KEY_COUNT;
    }

    static bool ParseSpecialKey(const char* keys, uint32_t index, char* name, uint32_t name_size, uint32_t* next_index)
    {
        if (index >= strlen(keys) || keys[index] != '{') return false;
        const char* start = keys + index + 1;
        const char* end = strchr(start, '}');
        if (!end) return false;
        uint32_t length = (uint32_t)(end - start);
        if (length == 0 || length >= name_size) return false;
        memcpy(name, start, length);
        name[length] = 0;
        *next_index = (uint32_t)(end - keys) + 1;
        return true;
    }

    static float ApplyEasing(float t, InputEasing easing)
    {
        t = ClampFloat(t, 0.0f, 1.0f);
        if (easing == INPUT_EASING_EASE_IN) return t * t;
        if (easing == INPUT_EASING_EASE_OUT) return 1.0f - (1.0f - t) * (1.0f - t);
        if (easing == INPUT_EASING_EASE_IN_OUT) return t < 0.5f ? 2.0f * t * t : 1.0f - 2.0f * (1.0f - t) * (1.0f - t);
        return t;
    }

    static void SamplePath(const InputEvent* event, float t, float* x, float* y)
    {
        if (event->m_PathMode == INPUT_PATH_SAMPLED)
        {
            const InputPoint* a = &event->m_Points.m_Data[event->m_Segment];
            const InputPoint* b = &event->m_Points.m_Data[event->m_Segment + 1];
            *x = a->m_X + (b->m_X - a->m_X) * t;
            *y = a->m_Y + (b->m_Y - a->m_Y) * t;
            return;
        }
        const InputPoint* p = event->m_Points.m_Data;
        float u = 1.0f - t;
        if (event->m_PathMode == INPUT_PATH_QUADRATIC)
        {
            *x = u * u * p[0].m_X + 2.0f * u * t * p[1].m_X + t * t * p[2].m_X;
            *y = u * u * p[0].m_Y + 2.0f * u * t * p[1].m_Y + t * t * p[2].m_Y;
            return;
        }
        *x = u * u * u * p[0].m_X + 3.0f * u * u * t * p[1].m_X + 3.0f * u * t * t * p[2].m_X + t * t * t * p[3].m_X;
        *y = u * u * u * p[0].m_Y + 3.0f * u * u * t * p[1].m_Y + 3.0f * u * t * t * p[2].m_Y + t * t * t * p[3].m_Y;
    }

    static void AddVisualizationPoint(float x, float y, bool reset)
    {
        InputVisualization* visualization = &g_AutomationBridge.m_InputVisualization;
        if (reset)
        {
            memset(visualization, 0, sizeof(*visualization));
            visualization->m_Active = true;
            visualization->m_Duration = INPUT_VISUALIZATION_SECONDS;
        }
        if (!visualization->m_Active) return;
        if (visualization->m_PointCount > 0)
        {
            uint32_t last = visualization->m_PointCount - 1;
            if (fabsf(visualization->m_X[last] - x) < 0.5f && fabsf(visualization->m_Y[last] - y) < 0.5f) return;
        }
        if (visualization->m_PointCount < MAX_INPUT_PATH_POINTS)
        {
            uint32_t i = visualization->m_PointCount++;
            visualization->m_X[i] = x;
            visualization->m_Y[i] = y;
            visualization->m_Drag = visualization->m_PointCount > 1;
        }
    }

    static bool InjectPointer(InputEvent* event, float x, float y, bool pressed, bool released, bool cancelled)
    {
        InputDevice device = event->m_Receipt.m_Device;
        if (device == INPUT_DEVICE_MOUSE)
        {
            dmHID::HMouse mouse = dmHID::GetMouse(g_AutomationBridge.m_HidContext, 0);
            if (mouse == dmHID::INVALID_MOUSE_HANDLE) return false;
            dmHID::SetMousePosition(mouse, (int32_t)x, (int32_t)y);
            dmHID::SetMouseButton(mouse, event->m_MouseButton, !(released || cancelled));
            return true;
        }
        if (device == INPUT_DEVICE_TOUCH)
        {
            dmHID::HTouchDevice touch = dmHID::GetTouchDevice(g_AutomationBridge.m_HidContext, 0);
            if (touch == dmHID::INVALID_TOUCH_DEVICE_HANDLE) return false;
            dmHID::Phase phase = pressed ? dmHID::PHASE_BEGAN : dmHID::PHASE_MOVED;
            if (released) phase = dmHID::PHASE_ENDED;
            if (cancelled) phase = dmHID::PHASE_CANCELLED;
            dmHID::AddTouch(touch, (int32_t)x, (int32_t)y, event->m_Receipt.m_PointerId, phase);
            return true;
        }
        return false;
    }

    static void StartReceipt(InputReceipt* receipt)
    {
        receipt->m_State = INPUT_STATE_STARTED;
        receipt->m_StartFrame = g_AutomationBridge.m_Frame;
        receipt->m_StartTime = dmTime::GetTime();
    }

    static void FinishReceipt(InputReceipt* receipt, InputState state, const char* reason)
    {
        receipt->m_State = state;
        receipt->m_ReleaseFrame = g_AutomationBridge.m_Frame;
        receipt->m_ReleaseTime = dmTime::GetTime();
        ReceiptSetReason(receipt, reason);
    }

    static bool UpdateMouseEvent(float dt, InputEvent* event)
    {
        InputPoint* first = &event->m_Points.m_Data[0];
        if (event->m_CancelRequested && !event->m_Pressed)
        {
            FinishReceipt(&event->m_Receipt, INPUT_STATE_CANCELLED, event->m_Receipt.m_Reason);
            return true;
        }
        if (!event->m_Pressed)
        {
            if (!InjectPointer(event, first->m_X, first->m_Y, true, false, false))
            {
                FinishReceipt(&event->m_Receipt, INPUT_STATE_FAILED, "input_device_unavailable");
                return true;
            }
            event->m_Pressed = true;
            event->m_Phase = event->m_HoldBefore > 0.0f ? 1 : (event->m_Points.m_Count > 1 ? 2 : 3);
            event->m_Elapsed = 0.0f;
            StartReceipt(&event->m_Receipt);
            if (event->m_Visualize) AddVisualizationPoint(first->m_X, first->m_Y, true);
            return false;
        }

        InputPoint* last = &event->m_Points.m_Data[event->m_Points.m_Count - 1];
        if (event->m_CancelRequested)
        {
            if (event->m_ReleaseOnCancel) InjectPointer(event, last->m_X, last->m_Y, false, false, true);
            FinishReceipt(&event->m_Receipt, INPUT_STATE_CANCELLED, event->m_Receipt.m_Reason);
            return true;
        }

        if (event->m_Phase == 1)
        {
            event->m_Elapsed += dt;
            if (event->m_Elapsed < event->m_HoldBefore) return false;
            event->m_Elapsed = 0.0f;
            event->m_Phase = event->m_Points.m_Count > 1 ? 2 : 3;
        }

        if (event->m_Phase == 2)
        {
            uint32_t duration_index = event->m_PathMode == INPUT_PATH_SAMPLED ? event->m_Segment + 1 : event->m_Points.m_Count - 1;
            InputPoint* duration_point = &event->m_Points.m_Data[duration_index];
            float duration = MaxFloat(0.0f, duration_point->m_Duration);
            event->m_Elapsed += dt;
            float t = duration <= 0.0f ? 1.0f : event->m_Elapsed / duration;
            bool segment_done = t >= 1.0f;
            t = ApplyEasing(t, duration_point->m_Easing);
            float x = 0.0f;
            float y = 0.0f;
            SamplePath(event, t, &x, &y);
            InjectPointer(event, x, y, false, false, false);
            if (event->m_Visualize) AddVisualizationPoint(x, y, false);
            if (!segment_done) return false;
            event->m_Elapsed = 0.0f;
            if (event->m_PathMode == INPUT_PATH_SAMPLED && event->m_Segment + 2 < event->m_Points.m_Count)
            {
                ++event->m_Segment;
                return false;
            }
            event->m_Phase = event->m_PointerOpen ? 4 : 3;
        }

        if (event->m_Phase == 4 && !event->m_ReleaseRequested) return false;
        if (event->m_Phase == 3 && event->m_HoldAfter > 0.0f)
        {
            event->m_Elapsed += dt;
            if (event->m_Elapsed < event->m_HoldAfter) return false;
        }
        if (event->m_PointerOpen && !event->m_ReleaseRequested) return false;

        InjectPointer(event, last->m_X, last->m_Y, false, true, false);
        FinishReceipt(&event->m_Receipt, INPUT_STATE_RELEASED, 0);
        return true;
    }

    static bool UpdateKeyEvent(InputEvent* event)
    {
        dmHID::HKeyboard keyboard = dmHID::GetKeyboard(g_AutomationBridge.m_HidContext, 0);
        if (keyboard == dmHID::INVALID_KEYBOARD_HANDLE)
        {
            FinishReceipt(&event->m_Receipt, INPUT_STATE_FAILED, "keyboard_unavailable");
            return true;
        }
        if (event->m_CancelRequested)
        {
            if (event->m_ActiveKey != dmHID::MAX_KEY_COUNT && event->m_ReleaseOnCancel)
                dmHID::SetKey(keyboard, event->m_ActiveKey, false);
            FinishReceipt(&event->m_Receipt, INPUT_STATE_CANCELLED, event->m_Receipt.m_Reason);
            return true;
        }
        if (event->m_Receipt.m_State == INPUT_STATE_ACCEPTED) StartReceipt(&event->m_Receipt);
        if (event->m_ActiveKey != dmHID::MAX_KEY_COUNT)
        {
            dmHID::SetKey(keyboard, event->m_ActiveKey, false);
            event->m_ActiveKey = dmHID::MAX_KEY_COUNT;
            if (event->m_KeyIndex >= strlen(event->m_Keys))
            {
                FinishReceipt(&event->m_Receipt, INPUT_STATE_RELEASED, 0);
                return true;
            }
            return false;
        }
        if (event->m_KeyIndex >= strlen(event->m_Keys))
        {
            FinishReceipt(&event->m_Receipt, INPUT_STATE_RELEASED, 0);
            return true;
        }
        char key_name[64];
        uint32_t next_index = event->m_KeyIndex;
        if (ParseSpecialKey(event->m_Keys, event->m_KeyIndex, key_name, sizeof(key_name), &next_index))
        {
            dmHID::Key key = KeyFromName(key_name);
            event->m_KeyIndex = next_index;
            if (key != dmHID::MAX_KEY_COUNT)
            {
                dmHID::SetKey(keyboard, key, true);
                event->m_ActiveKey = key;
            }
            return false;
        }
        const char* cursor = event->m_Keys + event->m_KeyIndex;
        uint32_t codepoint = dmUtf8::NextChar(&cursor);
        event->m_KeyIndex = (uint32_t)(cursor - event->m_Keys);
        if (codepoint != 0) dmHID::AddKeyboardChar(g_AutomationBridge.m_HidContext, (int)codepoint);
        if (event->m_KeyIndex >= strlen(event->m_Keys))
        {
            FinishReceipt(&event->m_Receipt, INPUT_STATE_RELEASED, 0);
            return true;
        }
        return false;
    }

    bool CancelInput(uint64_t input_id, bool release, const char* reason)
    {
        InputEvent* event = FindQueuedEvent(input_id, 0);
        if (!event) return false;
        event->m_CancelRequested = true;
        event->m_ReleaseOnCancel = release;
        ReceiptSetReason(&event->m_Receipt, reason);
        return true;
    }

    uint32_t FlushInput(const char* client_id, const char* session_id, bool release, const char* reason)
    {
        uint32_t count = 0;
        for (uint32_t i = 0; i < g_AutomationBridge.m_InputEvents.m_Count; ++i)
        {
            InputEvent* event = &g_AutomationBridge.m_InputEvents.m_Data[i];
            if (client_id && (!StringsEqual(event->m_Receipt.m_ClientId, client_id) ||
                              !StringsEqual(event->m_Receipt.m_SessionId, session_id)))
                continue;
            event->m_CancelRequested = true;
            event->m_ReleaseOnCancel = release;
            ReceiptSetReason(&event->m_Receipt, reason);
            ++count;
        }
        return count;
    }

    void UpdateInput(float dt)
    {
        if (!g_AutomationBridge.m_HidContext) return;
        uint64_t now = dmTime::GetTime();
        if (!IsEmpty(g_AutomationBridge.m_ControllerClientId) && g_AutomationBridge.m_ControllerLeaseDeadline <= now)
        {
            FlushInput(g_AutomationBridge.m_ControllerClientId, g_AutomationBridge.m_ControllerSessionId, true, "controller_lease_expired");
            ReleaseInputController(g_AutomationBridge.m_ControllerClientId, g_AutomationBridge.m_ControllerSessionId);
        }
        if (g_AutomationBridge.m_InputEvents.m_Count == 0) return;

        InputEvent* event = &g_AutomationBridge.m_InputEvents.m_Data[0];
        if (event->m_PointerOpen && event->m_LeaseDeadline <= now)
        {
            event->m_CancelRequested = true;
            event->m_ReleaseOnCancel = true;
            ReceiptSetReason(&event->m_Receipt, "pointer_lease_expired");
        }
        bool done = event->m_Type == INPUT_EVENT_KEYS ? UpdateKeyEvent(event) : UpdateMouseEvent(dt, event);
        if (done) RemoveFinishedEvent(0);
    }
}

#endif
