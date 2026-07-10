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
#include <dmsdk/dlib/mutex.h>

namespace dmAutomationBridge
{
    static const char* API_PREFIX = "/automation-bridge/v1";
    static const char* API_VERSION = "1";
    static const uint32_t MAX_INPUT_EVENTS = 64;
    static const uint32_t MAX_INPUT_HISTORY = 256;
    static const uint32_t MAX_INPUT_PATH_POINTS = 128;
    static const float MAX_INPUT_DURATION = 60.0f;
    static const uint32_t MAX_KEY_INPUT_BYTES = 4096;
    static const uint32_t MAX_APPLICATION_JSON_BYTES = 32768;
    static const uint32_t MAX_APPLICATION_NAME_BYTES = 128;
    static const uint32_t MAX_APPLICATION_JSON_DEPTH = 16;
    static const uint32_t MAX_JSON_REQUEST_BYTES = 64 * 1024;
    static const uint32_t MAX_JSON_NESTING = 16;

    template <typename T>
    struct Array
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
        char*                m_InstanceId;
        char*                m_LogicalId;
        char*                m_Name;
        char*                m_Type;
        char*                m_Kind;
        char*                m_Path;
        char*                m_Parent;
        char*                m_Text;
        char*                m_Url;
        char*                m_Resource;
        char*                m_AutomationId;
        char*                m_LocalizationKey;
        char*                m_Role;
        bool                 m_Visible;
        bool                 m_Enabled;
        Bounds               m_Bounds;
        Array<Property>       m_Properties;
        Array<uint32_t>       m_Children;
        uint64_t              m_CreatedSceneSequence;
        uint32_t              m_InstanceGeneration;
        bool                  m_HasInstanceIdentity;

        bool  m_HasPosition;
        bool  m_HasSize;
        float m_Position[3];
        float m_Size[3];
        float m_AnchorX;
        float m_AnchorY;
    };

    struct Snapshot
    {
        Array<Node>      m_Nodes;
        int32_t          m_Root;
        uint32_t         m_WindowWidth;
        uint32_t         m_WindowHeight;
        uint32_t         m_BackbufferWidth;
        uint32_t         m_BackbufferHeight;
        uint32_t         m_DisplayWidth;
        uint32_t         m_DisplayHeight;
        float            m_DisplayScale;
        int32_t          m_ViewportX;
        int32_t          m_ViewportY;
        uint32_t         m_ViewportWidth;
        uint32_t         m_ViewportHeight;
        uint64_t         m_Sequence;
    };

    enum ScreenshotState
    {
        SCREENSHOT_NONE,
        SCREENSHOT_PENDING,
        SCREENSHOT_COMPLETE,
        SCREENSHOT_FAILED
    };

    struct ScreenshotCapture
    {
        uint64_t        m_Id;
        ScreenshotState m_State;
        uint32_t        m_AfterFrames;
        uint64_t        m_Frame;
        uint64_t        m_SceneSequence;
        uint32_t        m_Width;
        uint32_t        m_Height;
        char            m_Path[1024];
        char            m_Sha256[65];
        char            m_Failure[128];
    };

    enum InputEventType
    {
        INPUT_EVENT_MOUSE,
        INPUT_EVENT_KEYS,
        INPUT_EVENT_POINTER
    };

    enum InputDevice
    {
        INPUT_DEVICE_AUTO,
        INPUT_DEVICE_MOUSE,
        INPUT_DEVICE_TOUCH
    };

    enum InputState
    {
        INPUT_STATE_ACCEPTED,
        INPUT_STATE_STARTED,
        INPUT_STATE_RELEASED,
        INPUT_STATE_CANCELLED,
        INPUT_STATE_FAILED
    };

    enum InputEasing
    {
        INPUT_EASING_LINEAR,
        INPUT_EASING_EASE_IN,
        INPUT_EASING_EASE_OUT,
        INPUT_EASING_EASE_IN_OUT
    };

    enum InputPathMode
    {
        INPUT_PATH_SAMPLED,
        INPUT_PATH_QUADRATIC,
        INPUT_PATH_CUBIC
    };

    struct InputPoint
    {
        float       m_X;
        float       m_Y;
        float       m_Duration;
        InputEasing m_Easing;
    };

    struct InputReceipt
    {
        uint64_t    m_Id;
        char*       m_Kind;
        InputState  m_State;
        uint64_t    m_AcceptedFrame;
        uint64_t    m_StartFrame;
        uint64_t    m_ReleaseFrame;
        uint64_t    m_AcceptedTime;
        uint64_t    m_StartTime;
        uint64_t    m_ReleaseTime;
        float       m_RequestedDuration;
        char*       m_Reason;
        char*       m_ClientId;
        char*       m_SessionId;
        char*       m_RequestId;
        uint64_t    m_SceneSequence;
        InputDevice m_Device;
        uint32_t    m_PointerId;
    };

    struct InputEvent
    {
        InputReceipt      m_Receipt;
        InputEventType     m_Type;
        Array<InputPoint>   m_Points;
        InputPathMode       m_PathMode;
        uint32_t            m_Segment;
        uint8_t             m_Phase;
        float              m_Elapsed;
        float              m_HoldBefore;
        float              m_HoldAfter;
        bool               m_Pressed;
        bool               m_ReleaseRequested;
        bool               m_CancelRequested;
        bool               m_ReleaseOnCancel;
        bool               m_PointerOpen;
        uint64_t           m_LeaseDeadline;
        dmHID::MouseButton m_MouseButton;
        bool               m_Visualize;

        char*      m_Keys;
        uint32_t   m_KeyIndex;
        dmHID::Key m_ActiveKey;
    };

    struct InputVisualization
    {
        bool  m_Active;
        bool  m_Drag;
        float m_X[MAX_INPUT_PATH_POINTS];
        float m_Y[MAX_INPUT_PATH_POINTS];
        uint32_t m_PointCount;
        float m_Age;
        float m_Duration;
        uint64_t m_LastRenderTime;
    };

    struct BridgeEvent
    {
        uint64_t m_Sequence;
        uint64_t m_Frame;
        uint64_t m_SceneSequence;
        uint64_t m_NativeTimestampUs;
        uint64_t m_RecordingTimestampUs;
        bool     m_HasRecordingTimestamp;
        char*    m_Type;
        char*    m_Name;
        char*    m_DataJson;
    };

    struct PublishedState
    {
        char*    m_Name;
        char*    m_ValueJson;
        uint64_t m_Revision;
        uint64_t m_Frame;
        uint64_t m_NativeTimestampUs;
    };

    struct CommandHandler
    {
        char*                      m_Name;
        dmScript::LuaCallbackInfo* m_Callback;
    };

    enum CommandState
    {
        COMMAND_PENDING,
        COMMAND_RUNNING,
        COMMAND_COMPLETED,
        COMMAND_FAILED,
        COMMAND_CANCELLED,
        COMMAND_TIMED_OUT
    };

    struct CommandInvocation
    {
        uint64_t     m_Id;
        CommandState m_State;
        char*        m_Name;
        char*        m_ArgumentsJson;
        char*        m_ResultJson;
        char*        m_Error;
        uint64_t     m_AcceptedTimestampUs;
        uint64_t     m_CompletedTimestampUs;
        uint64_t     m_DeadlineTimestampUs;
    };

    struct NodeAnnotation
    {
        char* m_NodeUrl;
        char* m_AutomationId;
        char* m_LocalizationKey;
        char* m_Role;
    };

    struct AutomationBridgeContext
    {
        bool                    m_Initialized;
        dmGameObject::HRegister m_Register;
        dmHID::HContext         m_HidContext;
        dmWebServer::HServer    m_WebServer;
        dmGraphics::HContext    m_GraphicsContext;
        void*                   m_RenderContext;
        bool                    m_WebHandlerRegistered;
        uint64_t                m_LastTime;
        uint64_t                m_Frame;
        uint64_t                m_SnapshotFrame;
        Snapshot                m_Snapshot;
        Array<InputEvent>      m_InputEvents;
        Array<InputReceipt>    m_InputHistory;
        InputVisualization      m_InputVisualization;
        uint64_t                m_NextInputId;
        char*                   m_ControllerClientId;
        char*                   m_ControllerSessionId;
        uint64_t                m_ControllerLeaseDeadline;
        InputDevice             m_DefaultInputDevice;
        bool                    m_DefaultInputVisualize;
        char                    m_EngineInstanceId[64];
        ScreenshotCapture       m_Screenshot;
        Array<ScreenshotCapture> m_ScreenshotHistory;
        uint64_t                m_ScreenshotCounter;
        uint32_t                m_DisplayWidth;
        uint32_t                m_DisplayHeight;
        bool                    m_ApplicationApiEnabled;
        bool                    m_CommandExecuting;
        dmMutex::HMutex         m_ApplicationMutex;
        Array<BridgeEvent>      m_Events;
        Array<PublishedState>   m_PublishedStates;
        Array<CommandHandler>   m_CommandHandlers;
        Array<CommandInvocation> m_CommandInvocations;
        Array<NodeAnnotation>   m_NodeAnnotations;
        uint32_t                m_EventCapacity;
        uint64_t                m_NextEventSequence;
        uint64_t                m_NextStateRevision;
        uint64_t                m_NextCommandId;
    };

    struct IncludeOptions
    {
        bool m_Bounds;
        bool m_Properties;
        bool m_Children;
    };

    typedef Array<uint8_t> ByteArray;

    extern AutomationBridgeContext g_AutomationBridge;

    template <typename T>
    static void ArrayInit(Array<T>* array)
    {
        memset(array, 0, sizeof(*array));
    }

    template <typename T>
    static void ArrayFree(Array<T>* array)
    {
        free(array->m_Data);
        memset(array, 0, sizeof(*array));
    }

    template <typename T>
    static bool ArrayReserve(Array<T>* array, uint32_t capacity)
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
    static bool ArraySetCount(Array<T>* array, uint32_t count)
    {
        if (!ArrayReserve(array, count))
        {
            return false;
        }
        array->m_Count = count;
        return true;
    }

    template <typename T>
    static bool ArrayPush(Array<T>* array, const T* value)
    {
        if (!ArrayReserve(array, array->m_Count + 1))
        {
            return false;
        }
        array->m_Data[array->m_Count++] = *value;
        return true;
    }

    template <typename T>
    static void ArrayErase(Array<T>* array, uint32_t index)
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
    void FreeQueryParams(Array<QueryParam>* query);
    void ParseResource(const char* resource, char** path, Array<QueryParam>* query);
    const char* GetParam(const Array<QueryParam>* query, const char* key);
    bool GetFloatParam(const Array<QueryParam>* query, const char* key, float* value);
    bool GetBoolParam(const Array<QueryParam>* query, const char* key, bool* value);
    bool ContainsCaseInsensitive(const char* haystack, const char* needle);
    bool ContainsCaseSensitive(const char* haystack, const char* needle);

    void InitSnapshot(Snapshot* snapshot);
    void FreeSnapshot(Snapshot* snapshot);
    void UpdateSnapshot();
    const Node* FindNodeById(const char* id);
    void AppendNodeJson(StringBuffer* out, const Snapshot* snapshot, const Node* node, const IncludeOptions* include, bool recursive, bool visible_only);
    void AppendScreenJson(StringBuffer* out, const Snapshot* snapshot);
    void ApplyNodeAnnotation(Node* node);

    void FreeInputReceipt(InputReceipt* receipt);
    void FreeInputEvent(InputEvent* event);
    const char* InputStateName(InputState state);
    const char* InputDeviceName(InputDevice device);
    bool ParseInputDevice(const char* value, InputDevice* device);
    bool ParseInputEasing(const char* value, InputEasing* easing);
    bool IsInputDeviceSupported(InputDevice device);
    InputDevice ResolveInputDevice(InputDevice device);
    bool AcquireInputController(const char* client_id, const char* session_id, float lease, const char** error);
    bool IsInputController(const char* client_id, const char* session_id);
    void ReleaseInputController(const char* client_id, const char* session_id);
    bool AddMouseInput(const Array<InputPoint>* points, InputPathMode path_mode, float hold_before, float hold_after,
                       InputDevice device, uint32_t pointer_id, bool visualize, const char* kind,
                       const char* client_id, const char* session_id, const char* request_id,
                       uint64_t scene_sequence, float lease, bool pointer_open, InputReceipt** receipt);
    bool AddKeyInput(const char* keys, const char* client_id, const char* session_id, const char* request_id,
                     uint64_t scene_sequence, InputReceipt** receipt);
    bool AppendPointerMove(uint64_t input_id, const InputPoint* point, float lease, const char** error);
    bool AppendPointerHold(uint64_t input_id, float duration, float lease, const char** error);
    bool ReleasePointer(uint64_t input_id, const char** error);
    const InputReceipt* FindInputReceipt(uint64_t input_id);
    void AppendInputReceiptJson(StringBuffer* out, const InputReceipt* receipt, uint32_t queue_position);
    bool CancelInput(uint64_t input_id, bool release, const char* reason);
    uint32_t FlushInput(const char* client_id, const char* session_id, bool release, const char* reason);
    bool GetNodeCenter(const char* id, float* x, float* y, const char** error);
    void UpdateInput(float dt);

    bool IsScreenshotSupported();
    bool BuildScreenshotPath(uint64_t capture_id, char* path, uint32_t path_size);
    bool ScheduleScreenshot(uint32_t after_frames, ScreenshotCapture* capture);
    void ProcessPendingScreenshot();
    void AppendScreenshotJson(StringBuffer* out, const ScreenshotCapture* capture);
    const ScreenshotCapture* FindScreenshotCapture(uint64_t capture_id);

    void InitApplicationBridge(uint32_t event_capacity, bool enabled);
    void FreeApplicationBridge();
    void FinalizeApplicationLua();
    void RegisterApplicationLua(lua_State* L);
    void UpdateApplicationBridge();
    bool EmitBridgeEvent(const char* type, const char* name, const char* data_json, uint64_t recording_timestamp_us, bool has_recording_timestamp);
    bool EmitBridgeEventWithReceipt(const char* type, const char* name, const char* data_json, uint64_t recording_timestamp_us, bool has_recording_timestamp, uint64_t* event_sequence, uint64_t* native_timestamp_us);
    uint64_t GetEventNextCursor();
    uint64_t GetEventOldestCursor();
    uint32_t AppendEventPageJson(StringBuffer* out, uint64_t cursor, uint32_t limit, bool* overflow, uint64_t* next_cursor);
    uint64_t GetStateRevision();
    uint32_t AppendPublishedStatesJson(StringBuffer* out, const char* name, uint64_t after_revision);
    bool SubmitCommand(const char* name, const char* arguments_json, uint32_t timeout_ms, uint64_t* command_id, const char** error);
    bool AppendCommandJson(StringBuffer* out, uint64_t command_id);
    bool CancelCommand(uint64_t command_id, const char** error);
    bool AddTimelineMarker(const char* name, const char* data_json, uint64_t recording_timestamp_us, bool has_recording_timestamp, uint64_t* event_sequence, uint64_t* native_timestamp_us);

    void RegisterWebEndpoint(dmExtension::AppParams* params);
    void UnregisterWebEndpoint();
    void AutomationBridgeHandler(void* user_data, dmWebServer::Request* request);
}

#endif
