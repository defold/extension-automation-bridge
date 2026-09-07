-- Opt-in fixtures for the sample project's native application catalog tests.
local M = {}

function M.register(current_items, item_bounds)
    automation_bridge.command("sample.arrange_items", function(data)
        -- Native input tests need separated targets clear of the SPAWN button.
        -- Keep randomized placement and motion in the interactive sample.
        local items = current_items()
        local ids = {}
        for id in pairs(items) do ids[#ids + 1] = id end
        table.sort(ids, function(a, b) return tostring(a) < tostring(b) end)
        local min_x, max_x, min_y, max_y = item_bounds()
        local columns = 3
        local rows = math.max(1, math.ceil(#ids / columns))
        for index, id in ipairs(ids) do
            local column = (index - 1) % columns
            local row = math.floor((index - 1) / columns)
            local x = min_x + (max_x - min_x) * (0.55 + 0.35 * column / (columns - 1))
            local y = min_y + (max_y - min_y) * (0.25 + 0.5 * row / math.max(1, rows - 1))
            items[id].pos = vmath.vector3(x, y, items[id].pos.z)
            items[id].velocity = vmath.vector3()
            go.set_position(items[id].pos, id)
        end
        return { arranged = #ids }
    end)
    automation_bridge.command("sample.catalog_probe", function(data)
        return { available = true }
    end)
    automation_bridge.describe("state", "sample.future", { description = "First declaration", schema = true })
    automation_bridge.describe("state", "sample.future", { description = "Replacement declaration", schema = false })

    local deep = { type = "object" }
    for _ = 1, 20 do
        deep = { properties = { child = deep } }
    end
    local invalid = {
        { "unknown", "sample.future", {} },
        { "state", "invalid name", {} },
        { "command", "sample.unregistered", { description = "Missing callback" } },
        { "state", "sample.future", { description = "" } },
        { "state", "sample.future", { description = 5 } },
        { "state", "sample.future", { description = string.rep("x", 4097) } },
        { "state", "sample.future", { schema = "object" } },
        { "state", "sample.future", { schema = { 1, 2 } } },
        { "state", "sample.future", { input_schema = {} } },
        { "command", "sample.catalog_probe", { schema = {} } },
        { "state", "sample.future", { unknown = true } },
        { "state", "sample.future", { schema = { description = string.rep("x", 32769) } } },
        { "state", "sample.future", { schema = deep } },
    }
    local rejected = {}
    for index, args in ipairs(invalid) do
        local ok = pcall(automation_bridge.describe, unpack(args))
        rejected[index] = not ok
    end
    automation_bridge.publish("sample.contract_checks", { rejected = rejected })
    automation_bridge.command("sample.catalog_reject", function(data)
        local ok = pcall(automation_bridge.describe, "state", "sample.future", { unknown = true })
        return { rejected = not ok }
    end)
end

return M
