#include "automation_bridge_private.h"

#if defined(DM_DEBUG)

#include <dmsdk/dlib/utf8.h>

#include <stdint.h>
#include <string.h>

namespace dmAutomationBridge
{
    void FreeInputEvent(InputEvent* event)
    {
        FreeString(&event->m_Keys);
    }

    static bool CanQueueInputEvent()
    {
        return g_AutomationBridge.m_InputEvents.m_Count < MAX_INPUT_EVENTS;
    }

    bool AddMouseInput(float x1, float y1, float x2, float y2, float duration)
    {
        if (!CanQueueInputEvent())
        {
            return false;
        }

        InputEvent event;
        memset(&event, 0, sizeof(event));
        event.m_Type = INPUT_EVENT_MOUSE;
        event.m_X1 = x1;
        event.m_Y1 = y1;
        event.m_X2 = x2;
        event.m_Y2 = y2;
        event.m_Duration = MaxFloat(0.0f, duration);
        event.m_MouseButton = dmHID::MOUSE_BUTTON_LEFT;
        event.m_ActiveKey = dmHID::MAX_KEY_COUNT;
        return ArrayPush(&g_AutomationBridge.m_InputEvents, &event);
    }

    bool AddKeyInput(const char* keys)
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
        if (!event.m_Keys || !ArrayPush(&g_AutomationBridge.m_InputEvents, &event))
        {
            FreeInputEvent(&event);
            return false;
        }
        return true;
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
        struct KeyMap
        {
            const char* m_Name;
            dmHID::Key  m_Key;
        };

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
            if (StringsEqual(name, keys[i].m_Name))
            {
                return keys[i].m_Key;
            }
        }

        if (strlen(name) == 5 && StartsWith(name, "KEY_") && name[4] >= 'A' && name[4] <= 'Z')
        {
            return (dmHID::Key)(dmHID::KEY_A + (name[4] - 'A'));
        }
        if (strlen(name) == 5 && StartsWith(name, "KEY_") && name[4] >= '0' && name[4] <= '9')
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

    static bool UpdateMouseEvent(dmHID::HMouse mouse, dmHID::HTouchDevice device, float dt, InputEvent* event)
    {
        bool instant = event->m_Duration <= 0.0f;
        bool pressed = event->m_Elapsed == 0.0f;
        bool released = instant ? !pressed : event->m_Elapsed >= event->m_Duration;
        float t = instant ? (pressed ? 0.0f : 1.0f) : event->m_Elapsed / event->m_Duration;
        t = ClampFloat(t, 0.0f, 1.0f);
        float x = event->m_X1 + (event->m_X2 - event->m_X1) * t;
        float y = event->m_Y1 + (event->m_Y2 - event->m_Y1) * t;

        if (instant && pressed)
        {
            event->m_Elapsed = 1.0f;
        }
        else
        {
            event->m_Elapsed += dt;
        }

        DoTouch(mouse, device, (int32_t)x, (int32_t)y, pressed, released);
        return released;
    }

    static bool ParseSpecialKey(const char* keys, uint32_t index, char* name, uint32_t name_size, uint32_t* next_index)
    {
        if (index >= strlen(keys) || keys[index] != '{')
        {
            return false;
        }

        const char* start = keys + index + 1;
        const char* end = strchr(start, '}');
        if (!end)
        {
            return false;
        }

        uint32_t length = (uint32_t)(end - start);
        if (length == 0 || length >= name_size)
        {
            return false;
        }

        memcpy(name, start, length);
        name[length] = 0;
        *next_index = (uint32_t)(end - keys) + 1;
        return true;
    }

    static bool UpdateKeyEvent(dmHID::HContext context, dmHID::HKeyboard keyboard, InputEvent* event)
    {
        if (event->m_ActiveKey != dmHID::MAX_KEY_COUNT)
        {
            if (keyboard != dmHID::INVALID_KEYBOARD_HANDLE)
            {
                dmHID::SetKey(keyboard, event->m_ActiveKey, false);
            }
            event->m_ActiveKey = dmHID::MAX_KEY_COUNT;
            return event->m_KeyIndex >= strlen(event->m_Keys);
        }

        if (event->m_KeyIndex >= strlen(event->m_Keys))
        {
            return true;
        }

        char key_name[64];
        uint32_t next_index = event->m_KeyIndex;
        if (ParseSpecialKey(event->m_Keys, event->m_KeyIndex, key_name, sizeof(key_name), &next_index))
        {
            dmHID::Key key = KeyFromName(key_name);
            event->m_KeyIndex = next_index;
            if (key != dmHID::MAX_KEY_COUNT && keyboard != dmHID::INVALID_KEYBOARD_HANDLE)
            {
                dmHID::SetKey(keyboard, key, true);
                event->m_ActiveKey = key;
                return false;
            }
            return event->m_KeyIndex >= strlen(event->m_Keys);
        }

        const char* cursor = event->m_Keys + event->m_KeyIndex;
        uint32_t codepoint = dmUtf8::NextChar(&cursor);
        event->m_KeyIndex = (uint32_t)(cursor - event->m_Keys);
        if (codepoint != 0)
        {
            dmHID::AddKeyboardChar(context, (int)codepoint);
        }
        return event->m_KeyIndex >= strlen(event->m_Keys);
    }

    void UpdateInput(float dt)
    {
        if (!g_AutomationBridge.m_HidContext)
        {
            return;
        }

        dmHID::HMouse mouse = dmHID::GetMouse(g_AutomationBridge.m_HidContext, 0);
        dmHID::HKeyboard keyboard = dmHID::GetKeyboard(g_AutomationBridge.m_HidContext, 0);
        dmHID::HTouchDevice touch_device = dmHID::INVALID_TOUCH_DEVICE_HANDLE;
#if defined(DM_PLATFORM_IOS) || defined(DM_PLATFORM_ANDROID) || defined(DM_PLATFORM_SWITCH)
        touch_device = dmHID::GetTouchDevice(g_AutomationBridge.m_HidContext, 0);
#endif

        for (uint32_t i = 0; i < g_AutomationBridge.m_InputEvents.m_Count;)
        {
            bool done = false;
            InputEvent* event = &g_AutomationBridge.m_InputEvents.m_Data[i];
            if (event->m_Type == INPUT_EVENT_MOUSE)
            {
                done = UpdateMouseEvent(mouse, touch_device, dt, event);
            }
            else if (event->m_Type == INPUT_EVENT_KEYS)
            {
                done = UpdateKeyEvent(g_AutomationBridge.m_HidContext, keyboard, event);
            }

            if (done)
            {
                FreeInputEvent(event);
                ArrayErase(&g_AutomationBridge.m_InputEvents, i);
            }
            else
            {
                ++i;
            }
        }
    }


}

#endif
