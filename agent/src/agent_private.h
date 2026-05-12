#pragma once

#if !defined(DM_RELEASE) && !defined(DM_HEADLESS) && !defined(DM_DEBUG)
#define DM_DEBUG
#endif

#include <dmsdk/sdk.h>

#if defined(DM_DEBUG)

#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

namespace dmAgent
{
    static const char* API_PREFIX = "/agent/v1";
    static const char* API_VERSION = "1";
    static const uint32_t MAX_INPUT_EVENTS = 64;
    static const uint32_t MAX_KEY_INPUT_BYTES = 4096;

    template <typename T>
    struct AgentArray
    {
        T*       m_Data;
        uint32_t m_Count;
        uint32_t m_Capacity;
    };

    struct StringBuffer
    {
        char*    m_Data;
        uint32_t m_Size;
        uint32_t m_Capacity;
        bool     m_Failed;
    };

    struct QueryParam
    {
        char* m_Key;
        char* m_Value;
    };

    struct Property
    {
        char*   m_Name;
        char*   m_Json;
        char*   m_StringValue;
        float   m_Vector[4];
        uint8_t m_VectorCount;
        bool    m_HasVector;
        bool    m_BoolValue;
        bool    m_HasBool;
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
    };

    struct Node
    {
        char*                m_Id;
        char*                m_Name;
        char*                m_Type;
        char*                m_Kind;
        char*                m_Path;
        char*                m_Parent;
        char*                m_Text;
        char*                m_Url;
        char*                m_Resource;
        bool                 m_Visible;
        bool                 m_Enabled;
        Bounds               m_Bounds;
        AgentArray<Property> m_Properties;
        AgentArray<uint32_t> m_Children;

        bool  m_HasPosition;
        bool  m_HasSize;
        float m_Position[3];
        float m_Size[3];
        float m_AnchorX;
        float m_AnchorY;
    };

    struct Snapshot
    {
        AgentArray<Node> m_Nodes;
        int32_t          m_Root;
        uint32_t         m_WindowWidth;
        uint32_t         m_WindowHeight;
        uint32_t         m_BackbufferWidth;
        uint32_t         m_BackbufferHeight;
        int32_t          m_ViewportX;
        int32_t          m_ViewportY;
        uint32_t         m_ViewportWidth;
        uint32_t         m_ViewportHeight;
        uint64_t         m_Sequence;
    };

    enum InputEventType
    {
        INPUT_EVENT_MOUSE,
        INPUT_EVENT_KEYS
    };

    struct InputEvent
    {
        InputEventType     m_Type;
        float              m_X1;
        float              m_Y1;
        float              m_X2;
        float              m_Y2;
        float              m_Duration;
        float              m_Elapsed;
        dmHID::MouseButton m_MouseButton;

        char*      m_Keys;
        uint32_t   m_KeyIndex;
        dmHID::Key m_ActiveKey;
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
        AgentArray<InputEvent>  m_InputEvents;
        bool                    m_ScreenshotPending;
        uint32_t                m_ScreenshotCounter;
        char                    m_ScreenshotPath[1024];
    };

    struct IncludeOptions
    {
        bool m_Bounds;
        bool m_Properties;
        bool m_Children;
    };

    typedef AgentArray<uint8_t> ByteArray;

    extern AgentContext g_Agent;

    template <typename T>
    static void ArrayInit(AgentArray<T>* array)
    {
        memset(array, 0, sizeof(*array));
    }

    template <typename T>
    static void ArrayFree(AgentArray<T>* array)
    {
        free(array->m_Data);
        memset(array, 0, sizeof(*array));
    }

    template <typename T>
    static bool ArrayReserve(AgentArray<T>* array, uint32_t capacity)
    {
        if (capacity <= array->m_Capacity)
        {
            return true;
        }

        uint32_t new_capacity = array->m_Capacity ? array->m_Capacity : 8;
        while (new_capacity < capacity)
        {
            if (new_capacity > 0x7fffffffU / 2)
            {
                new_capacity = capacity;
                break;
            }
            new_capacity *= 2;
        }

        if (new_capacity > 0xffffffffU / (uint32_t)sizeof(T))
        {
            return false;
        }

        void* data = realloc(array->m_Data, (size_t)new_capacity * sizeof(T));
        if (!data)
        {
            return false;
        }

        array->m_Data = (T*)data;
        array->m_Capacity = new_capacity;
        return true;
    }

    template <typename T>
    static bool ArraySetCount(AgentArray<T>* array, uint32_t count)
    {
        if (!ArrayReserve(array, count))
        {
            return false;
        }
        array->m_Count = count;
        return true;
    }

    template <typename T>
    static bool ArrayPush(AgentArray<T>* array, const T* value)
    {
        if (!ArrayReserve(array, array->m_Count + 1))
        {
            return false;
        }
        array->m_Data[array->m_Count++] = *value;
        return true;
    }

    template <typename T>
    static void ArrayErase(AgentArray<T>* array, uint32_t index)
    {
        if (index + 1 < array->m_Count)
        {
            memmove(array->m_Data + index, array->m_Data + index + 1, (size_t)(array->m_Count - index - 1) * sizeof(T));
        }
        --array->m_Count;
    }

    static inline float MinFloat(float a, float b)
    {
        return a < b ? a : b;
    }

    static inline float MaxFloat(float a, float b)
    {
        return a > b ? a : b;
    }

    static inline float ClampFloat(float value, float min_value, float max_value)
    {
        return MaxFloat(min_value, MinFloat(value, max_value));
    }

    static inline bool IsEmpty(const char* value)
    {
        return value == 0 || value[0] == 0;
    }

    static inline bool StringsEqual(const char* a, const char* b)
    {
        if (a == 0)
        {
            a = "";
        }
        if (b == 0)
        {
            b = "";
        }
        return strcmp(a, b) == 0;
    }

    static inline bool StartsWith(const char* value, const char* prefix)
    {
        if (!value || !prefix)
        {
            return false;
        }
        size_t prefix_len = strlen(prefix);
        return strncmp(value, prefix, prefix_len) == 0;
    }

    static inline bool IsFiniteFloat(float value)
    {
        return isfinite(value) != 0;
    }

    static inline bool IsFiniteDouble(double value)
    {
        return isfinite(value) != 0;
    }

    char* DuplicateStringN(const char* value, uint32_t length);
    char* DuplicateString(const char* value);
    bool SetString(char** target, const char* value);
    void FreeString(char** value);
    void StringBufferInit(StringBuffer* buffer);
    void StringBufferFree(StringBuffer* buffer);
    bool StringBufferReserve(StringBuffer* buffer, uint32_t size);
    void StringBufferAppendN(StringBuffer* buffer, const char* value, uint32_t length);
    void StringBufferAppend(StringBuffer* buffer, const char* value);
    void StringBufferAppendChar(StringBuffer* buffer, char value);
    char* StringBufferDetach(StringBuffer* buffer);
    void AppendNumber(StringBuffer* out, double value);
    void AppendJsonString(StringBuffer* out, const char* value);
    bool SetHashString(char** out, dmhash_t hash);
    void FreeQueryParams(AgentArray<QueryParam>* query);
    void ParseResource(const char* resource, char** path, AgentArray<QueryParam>* query);
    const char* GetParam(const AgentArray<QueryParam>* query, const char* key);
    bool GetFloatParam(const AgentArray<QueryParam>* query, const char* key, float* value);
    bool GetBoolParam(const AgentArray<QueryParam>* query, const char* key, bool* value);
    bool ContainsCaseInsensitive(const char* haystack, const char* needle);

    void InitSnapshot(Snapshot* snapshot);
    void FreeSnapshot(Snapshot* snapshot);
    void UpdateSnapshot();
    const Node* FindNodeById(const char* id);
    void AppendNodeJson(StringBuffer* out, const Snapshot* snapshot, const Node* node, const IncludeOptions* include, bool recursive, bool visible_only);
    void AppendScreenJson(StringBuffer* out, const Snapshot* snapshot);

    void FreeInputEvent(InputEvent* event);
    bool AddMouseInput(float x1, float y1, float x2, float y2, float duration);
    bool AddKeyInput(const char* keys);
    bool GetNodeCenter(const char* id, float* x, float* y, const char** error);
    void UpdateInput(float dt);

    bool IsScreenshotSupported();
    bool BuildScreenshotPath(char* path, uint32_t path_size);
    bool ScheduleScreenshot(const char* path);
    void ProcessPendingScreenshot();

    void RegisterWebEndpoint(dmExtension::AppParams* params);
    void UnregisterWebEndpoint();
    void AgentHandler(void* user_data, dmWebServer::Request* request);
}

#endif
