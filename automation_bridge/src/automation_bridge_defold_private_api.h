#pragma once

#include "automation_bridge_private.h"

#if defined(DM_DEBUG)

namespace dmAutomationBridge
{
    // Defold private API adapter.
    //
    // This file intentionally isolates calls into Defold APIs that are not part
    // of the stable public extension SDK. Keep all private render/gui access
    // behind this small wrapper so the rest of Automation Bridge only depends
    // on high-level helpers and can fall back when internals change.
    void DefoldPrivateApiInitialize(dmExtension::Params* params);
    bool DefoldPrivateApiSetWindowSize(uint32_t width, uint32_t height);
    bool DefoldPrivateApiComputeBounds(const dmGameObject::SceneNode* scene_node, const Node* node, const Snapshot* snapshot, Bounds* out_bounds);
}

#endif
