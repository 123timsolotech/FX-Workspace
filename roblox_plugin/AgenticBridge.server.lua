--[[
	Agentic Bridge - Roblox Studio plugin for Agentic Workspace (v3)
	=================================================================
	Connects Studio to the local assistant running at http://127.0.0.1:8000.

	INSTALL
	  1. Copy this file into Studio's local plugins folder:
	       %LOCALAPPDATA%\Roblox\Plugins\AgenticBridge.server.lua
	     (Studio: Plugins tab -> Plugins Folder opens it.)
	  2. Restart Studio. An "Agentic Workspace" toolbar appears on the Plugins tab.
	  3. Click "Bridge" once. Studio asks for HTTP permission to 127.0.0.1 - allow it.
	     The on/off choice is remembered, so it reconnects on every place load.

	WHAT IT DOES
	  * Long-polls the assistant for a small, fixed set of commands (no arbitrary
	    code execution - there is intentionally no loadstring path).
	  * Every change is wrapped in a ChangeHistoryService recording, so one Ctrl+Z
	    undoes whatever the assistant did. Script edits are also kept in the
	    assistant's own undo trail, and a script edit is refused if the script
	    changed since the assistant read it.
	  * Every few seconds it pushes a tiny context snapshot (place name, selection,
	    open script, play state, new Output errors - with stack traces during
	    play tests). That is what lets the assistant offer help on its own.
	    Nothing leaves your machine: the only endpoint is 127.0.0.1.
	  * v3: each copy (edit window, play-test server, play-test client) has its
	    own id, so several Studio windows never mix up their commands; play tests
	    started by the assistant end themselves after the requested time.
]]

local HttpService = game:GetService("HttpService")
local RunService = game:GetService("RunService")
local Selection = game:GetService("Selection")
local LogService = game:GetService("LogService")
local ChangeHistoryService = game:GetService("ChangeHistoryService")
local ScriptContext = game:GetService("ScriptContext")

local VERSION = 3
local INSTANCE = HttpService:GenerateGUID(false)
local BASE_URL = plugin:GetSetting("AgenticBridgeUrl") or "http://127.0.0.1:8000"
local POLL_WAIT = 15
local CONTEXT_INTERVAL = 3
local OUTPUT_MAX = 300

-- The place's real name. For a place opened from Roblox (not a file) Studio
-- keeps game.Name as "Place1" while its window says "Test1"; the published
-- name comes from the place id. Looked up once per place id, cached.
local placeNameCache = {}
local function placeName()
	local id = game.PlaceId
	if id == 0 then
		return game.Name
	end
	if placeNameCache[id] == nil then
		local ok, info = pcall(function()
			return game:GetService("MarketplaceService"):GetProductInfo(id)
		end)
		placeNameCache[id] = (ok and type(info) == "table" and info.Name and info.Name ~= "") and info.Name or false
	end
	return placeNameCache[id] or game.Name
end

local IS_EDIT = RunService:IsEdit()
local ROLE = IS_EDIT and "edit" or (RunService:IsServer() and "play-server" or "play-client")

-- ---------------------------------------------------------------------------
-- Output capture (ring buffer + "new errors since last push")
-- ---------------------------------------------------------------------------
local output = {}
local pendingErrors = {}
local counts = { errors = 0, warnings = 0 }

-- Output written before this plugin loaded (only for reading the Output;
-- these are not reported as new errors).
pcall(function()
	for _, entry in ipairs(LogService:GetLogHistory()) do
		local kind = "info"
		if entry.messageType == Enum.MessageType.MessageError then
			kind = "error"
		elseif entry.messageType == Enum.MessageType.MessageWarning then
			kind = "warning"
		end
		table.insert(output, { kind = kind, text = string.sub(entry.message, 1, 800), at = entry.timestamp })
	end
	while #output > OUTPUT_MAX do
		table.remove(output, 1)
	end
end)

-- One error can arrive twice (Output line + ScriptContext.Error with its stack
-- trace): merge them.
local function addError(text, trace, scriptPath)
	for i = #pendingErrors, math.max(1, #pendingErrors - 5), -1 do
		local e = pendingErrors[i]
		if e.text == text or string.find(text, e.text, 1, true) or string.find(e.text, text, 1, true) then
			e.trace = e.trace or trace
			e.script = e.script or scriptPath
			return
		end
	end
	if #pendingErrors < 50 then
		table.insert(pendingErrors, { text = text, trace = trace, script = scriptPath })
	end
end

LogService.MessageOut:Connect(function(message, messageType)
	local kind = "info"
	if messageType == Enum.MessageType.MessageError then
		kind = "error"
		counts.errors += 1
		addError(message, nil, nil)
	elseif messageType == Enum.MessageType.MessageWarning then
		kind = "warning"
		counts.warnings += 1
	end
	table.insert(output, { kind = kind, text = string.sub(message, 1, 800), at = os.time() })
	if #output > OUTPUT_MAX then
		table.remove(output, 1)
	end
end)

-- ---------------------------------------------------------------------------
-- Paths:  "Workspace/Map/Door"  (also accepts "game/Workspace/..." and, when
-- no "/" is present, the dotted form GetFullName() produces).
-- ---------------------------------------------------------------------------
local function pathOf(inst)
	if inst == nil then
		return nil
	end
	if inst == game then
		return "game"
	end
	local parts = {}
	local node = inst
	while node and node ~= game do
		table.insert(parts, 1, node.Name)
		node = node.Parent
	end
	return table.concat(parts, "/")
end

local function childNamed(parent, name)
	if parent == game then
		local ok, service = pcall(function()
			return game:GetService(name)
		end)
		if ok and service then
			return service
		end
	end
	return parent:FindFirstChild(name)
end

local function resolve(path)
	if path == nil or path == "" or path == "game" then
		return game
	end
	local sep = string.find(path, "/", 1, true) and "/" or "."
	local node = game
	for segment in string.gmatch(path, "[^" .. (sep == "." and "%." or "/") .. "]+") do
		if not (node == game and string.lower(segment) == "game") then
			node = childNamed(node, segment)
			if node == nil then
				return nil
			end
		end
	end
	return node
end

-- ---------------------------------------------------------------------------
-- Value conversion
-- ---------------------------------------------------------------------------
local function num(t, ...)
	for _, key in ipairs({ ... }) do
		if type(t[key]) == "number" then
			return t[key]
		end
	end
	return 0
end

local function toColor3(value)
	if type(value) == "string" then
		local hex = string.gsub(value, "#", "")
		if #hex == 6 then
			return Color3.fromHex(hex)
		end
		return BrickColor.new(value).Color
	end
	local r = value.r or value.R or value[1] or 0
	local g = value.g or value.G or value[2] or 0
	local b = value.b or value.B or value[3] or 0
	if r > 1 or g > 1 or b > 1 then
		return Color3.fromRGB(r, g, b)
	end
	return Color3.new(r, g, b)
end

local function coerce(inst, prop, value)
	local ok, current = pcall(function()
		return inst[prop]
	end)
	local kind = ok and typeof(current) or "nil"
	if kind == "Vector3" and type(value) == "table" then
		return Vector3.new(value.x or value.X or value[1] or 0, value.y or value.Y or value[2] or 0, value.z or value.Z or value[3] or 0)
	elseif kind == "Vector2" and type(value) == "table" then
		return Vector2.new(value.x or value.X or value[1] or 0, value.y or value.Y or value[2] or 0)
	elseif kind == "Color3" then
		return toColor3(value)
	elseif kind == "BrickColor" then
		return BrickColor.new(value)
	elseif kind == "EnumItem" and type(value) == "string" then
		local name = string.match(value, "([%w_]+)$")
		return current.EnumType[name]
	elseif kind == "CFrame" and type(value) == "table" then
		if #value == 12 then
			return CFrame.new(table.unpack(value))
		end
		local p = value.position or value.Position or value
		local base = CFrame.new(p.x or p.X or p[1] or 0, p.y or p.Y or p[2] or 0, p.z or p.Z or p[3] or 0)
		local o = value.orientation or value.Orientation
		if o then
			base = base * CFrame.fromOrientation(math.rad(num(o, "x", "X", 1)), math.rad(num(o, "y", "Y", 2)), math.rad(num(o, "z", "Z", 3)))
		end
		return base
	elseif kind == "UDim2" and type(value) == "table" then
		return UDim2.new(value.xs or value[1] or 0, value.xo or value[2] or 0, value.ys or value[3] or 0, value.yo or value[4] or 0)
	elseif kind == "UDim" and type(value) == "table" then
		return UDim.new(value.s or value[1] or 0, value.o or value[2] or 0)
	elseif kind == "NumberRange" and type(value) == "table" then
		return NumberRange.new(value[1] or 0, value[2] or value[1] or 0)
	elseif (kind == "Instance" or kind == "nil") and type(value) == "string" and string.find(value, "/", 1, true) then
		local target = resolve(value)
		if target then
			return target
		end
	end
	return value
end

local function serialize(value)
	local kind = typeof(value)
	if kind == "string" or kind == "number" or kind == "boolean" then
		return value
	elseif kind == "Vector3" then
		return { x = value.X, y = value.Y, z = value.Z }
	elseif kind == "Vector2" then
		return { x = value.X, y = value.Y }
	elseif kind == "Color3" then
		return { r = math.floor(value.R * 255 + 0.5), g = math.floor(value.G * 255 + 0.5), b = math.floor(value.B * 255 + 0.5), hex = "#" .. value:ToHex() }
	elseif kind == "CFrame" then
		local rx, ry, rz = value:ToOrientation()
		return { position = serialize(value.Position), orientation = { x = math.deg(rx), y = math.deg(ry), z = math.deg(rz) } }
	elseif kind == "BrickColor" then
		return value.Name
	elseif kind == "EnumItem" then
		return tostring(value)
	elseif kind == "Instance" then
		return pathOf(value)
	elseif value == nil then
		return nil
	end
	return tostring(value)
end

local DEFAULT_PROPS = {
	BasePart = { "Position", "Size", "Orientation", "Color", "Material", "Anchored", "CanCollide", "Transparency" },
	LuaSourceContainer = { "Enabled", "RunContext" },
	GuiObject = { "Position", "Size", "Visible", "BackgroundColor3", "BackgroundTransparency" },
	Model = { "PrimaryPart", "WorldPivot" },
}

-- ---------------------------------------------------------------------------
-- Undo wrapper: every mutation is one ChangeHistory step.
-- ---------------------------------------------------------------------------
local function recorded(name, fn)
	local recording
	pcall(function()
		recording = ChangeHistoryService:TryBeginRecording("Agentic: " .. name)
	end)
	local ok, result = pcall(fn)
	if recording then
		pcall(function()
			ChangeHistoryService:FinishRecording(recording, ok and Enum.FinishRecordingOperation.Commit or Enum.FinishRecordingOperation.Cancel)
		end)
	else
		pcall(function()
			ChangeHistoryService:SetWaypoint("Agentic: " .. name)
		end)
	end
	if not ok then
		error(result, 0)
	end
	return result
end

local function sourceOf(script)
	local ok, source = pcall(function()
		return game:GetService("ScriptEditorService"):GetEditorSource(script)
	end)
	if ok and type(source) == "string" then
		return source
	end
	return script.Source
end

local function activeScript()
	local ok, active = pcall(function()
		return game:GetService("StudioService").ActiveScript
	end)
	return ok and active or nil
end

if not IS_EDIT then
	-- During a play test: structured errors with stack traces.
	pcall(function()
		ScriptContext.Error:Connect(function(message, trace, script)
			addError(message, trace, script and pathOf(script) or nil)
		end)
	end)
end

local SERVICE_ORDER = { "Workspace", "ServerScriptService", "ServerStorage", "ReplicatedStorage", "ReplicatedFirst", "StarterGui", "StarterPack", "StarterPlayer", "Lighting", "SoundService", "Teams", "Players" }
local PROTECTED = { Workspace = true, Lighting = true, ReplicatedStorage = true, ServerScriptService = true, ServerStorage = true, StarterGui = true, StarterPack = true, StarterPlayer = true, Players = true, Teams = true, SoundService = true, ReplicatedFirst = true }

-- ---------------------------------------------------------------------------
-- Command handlers. Each returns (data, message) or errors.
-- ---------------------------------------------------------------------------
local handlers = {}

function handlers.ping()
	return { version = VERSION, place = placeName() }, "pong"
end

function handlers.get_selection()
	local items = {}
	for _, inst in ipairs(Selection:Get()) do
		table.insert(items, { name = inst.Name, className = inst.ClassName, path = pathOf(inst) })
	end
	return items, (#items == 0) and "Nothing is selected." or (#items .. " selected.")
end

function handlers.list_children(p)
	local node = resolve(p.path)
	assert(node, "No instance at path '" .. tostring(p.path) .. "'.")
	local items = {}
	for i, child in ipairs(node:GetChildren()) do
		if i > 200 then
			break
		end
		table.insert(items, { name = child.Name, className = child.ClassName, childCount = #child:GetChildren(), path = pathOf(child) })
	end
	return items, #items .. " children of " .. pathOf(node)
end

function handlers.get_properties(p)
	local node = resolve(p.path)
	assert(node, "No instance at path '" .. tostring(p.path) .. "'.")
	local names = p.properties
	if type(names) ~= "table" or #names == 0 then
		names = { "Name", "ClassName" }
		for className, extra in pairs(DEFAULT_PROPS) do
			if node:IsA(className) then
				for _, n in ipairs(extra) do
					table.insert(names, n)
				end
			end
		end
	end
	local out = { path = pathOf(node), parent = pathOf(node.Parent) }
	for _, name in ipairs(names) do
		local ok, value = pcall(function()
			return node[name]
		end)
		out[name] = ok and serialize(value) or "<unreadable>"
	end
	return out, "Read " .. #names .. " properties."
end

-- Script code only changes through the checked, undoable script commands.
local SCRIPT_CLASSES = { Script = true, LocalScript = true, ModuleScript = true }
local SOURCE_PROPS = { Source = true, LinkedSource = true }

function handlers.create_instance(p)
	assert(not SCRIPT_CLASSES[p.className], "Scripts are created with roblox_create_script, not create_instance.")
	for prop in pairs(p.properties or {}) do
		assert(not SOURCE_PROPS[prop], "Script code is changed with roblox_edit_script, not as a property.")
	end
	local parent = resolve(p.parentPath)
	assert(parent, "Parent path '" .. tostring(p.parentPath) .. "' not found.")
	local inst = recorded("create " .. tostring(p.className), function()
		local created = Instance.new(p.className)
		if p.name then
			created.Name = p.name
		end
		for prop, value in pairs(p.properties or {}) do
			created[prop] = coerce(created, prop, value)
		end
		created.Parent = parent
		return created
	end)
	Selection:Set({ inst })
	return { path = pathOf(inst) }, "Created " .. inst.ClassName .. " at " .. pathOf(inst)
end

function handlers.set_property(p)
	assert(not SOURCE_PROPS[p.property], "Script code is changed with roblox_edit_script, not set_property.")
	local node = resolve(p.path)
	assert(node, "No instance at path '" .. tostring(p.path) .. "'.")
	local before = serialize((select(2, pcall(function()
		return node[p.property]
	end))))
	recorded("set " .. tostring(p.property), function()
		node[p.property] = coerce(node, p.property, p.value)
	end)
	local after = serialize(node[p.property])
	return { path = pathOf(node), before = before, after = after }, "Set " .. p.property .. " on " .. pathOf(node)
end

function handlers.create_script(p)
	local parent = resolve(p.parentPath)
	assert(parent, "Parent path '" .. tostring(p.parentPath) .. "' not found.")
	local className = p.className or "Script"
	assert(className == "Script" or className == "LocalScript" or className == "ModuleScript", "className must be Script, LocalScript or ModuleScript.")
	local script = recorded("create " .. className, function()
		local s = Instance.new(className)
		s.Name = p.name or className
		s.Source = p.source or ""
		s.Parent = parent
		return s
	end)
	Selection:Set({ script })
	return { path = pathOf(script) }, "Created " .. className .. " at " .. pathOf(script)
end

function handlers.delete_instance(p)
	local node = resolve(p.path)
	assert(node, "No instance at path '" .. tostring(p.path) .. "'.")
	assert(node ~= game and node.Parent ~= game, "Refusing to delete a top-level service.")
	local path = pathOf(node)
	recorded("delete " .. node.Name, function()
		node.Parent = nil -- undoable, unlike Destroy()
	end)
	return { path = path }, "Deleted " .. path .. " (Ctrl+Z restores it)."
end

function handlers.read_script(p)
	local node = p.path and resolve(p.path) or activeScript()
	assert(node and node:IsA("LuaSourceContainer"), "No script at '" .. tostring(p.path) .. "'.")
	local source = sourceOf(node)
	local lines = string.split(source, "\n")
	local first = math.max(1, tonumber(p.startLine) or 1)
	local last = math.min(#lines, tonumber(p.endLine) or #lines)
	local numbered = {}
	for i = first, last do
		table.insert(numbered, string.format("%4d  %s", i, lines[i]))
	end
	local text = table.concat(numbered, "\n")
	if #text > 60000 then
		text = string.sub(text, 1, 60000) .. "\n... [truncated - request a smaller line range]"
	end
	return { path = pathOf(node), lineCount = #lines, startLine = first, endLine = last, source = text }, text
end

function handlers.get_script_source(p)
	local node = (p.path and p.path ~= "") and resolve(p.path) or activeScript()
	assert(node and node:IsA("LuaSourceContainer"), "No script at '" .. tostring(p.path) .. "'.")
	local source = sourceOf(node)
	return { path = pathOf(node), source = source, lineCount = #string.split(source, "\n") }, "ok"
end

function handlers.set_script_source(p)
	local node = resolve(p.path)
	assert(node and node:IsA("LuaSourceContainer"), "No script at '" .. tostring(p.path) .. "'.")
	assert(type(p.source) == "string", "source must be a string.")
	-- Compare-and-set: never overwrite an edit made after the assistant read the script.
	if type(p.expected) == "string" and sourceOf(node) ~= p.expected then
		error("The script changed since the assistant read it (it was edited in Studio). Nothing was written - read it again.", 0)
	end
	recorded("edit " .. node.Name, function()
		local ok = pcall(function()
			game:GetService("ScriptEditorService"):UpdateSourceAsync(node, function()
				return p.source
			end)
		end)
		if not ok then
			node.Source = p.source
		end
	end)
	return { path = pathOf(node), lineCount = #string.split(p.source, "\n") }, "Updated " .. pathOf(node)
end

function handlers.get_tree(p)
	local depthMax = math.clamp(tonumber(p.depth) or 3, 1, 6)
	local limit = math.clamp(tonumber(p.limit) or 200, 1, 400)
	local roots = {}
	if p.root == nil or p.root == "" or p.root == "game" then
		for _, name in ipairs(SERVICE_ORDER) do
			local ok, service = pcall(function()
				return game:GetService(name)
			end)
			if ok and service then
				table.insert(roots, service)
			end
		end
	else
		local node = resolve(p.root)
		assert(node, "No instance at path '" .. tostring(p.root) .. "'.")
		table.insert(roots, node)
	end
	local items = {}
	local function walk(inst, depth)
		if #items >= limit then
			return
		end
		table.insert(items, { path = pathOf(inst), className = inst.ClassName, depth = depth, childCount = #inst:GetChildren() })
		if depth + 1 < depthMax then
			for _, child in ipairs(inst:GetChildren()) do
				if #items >= limit then
					return
				end
				walk(child, depth + 1)
			end
		end
	end
	for _, root in ipairs(roots) do
		walk(root, 0)
	end
	return items, #items .. " item(s)."
end

-- Starts a play test and waits for it to end. The play-test server copy of
-- this plugin ends it after `seconds` (see the end of this file). Only the edit
-- copy takes commands, so nothing else runs meanwhile.
function handlers.run_playtest(p)
	assert(IS_EDIT, "Play tests can only be started from the edit window.")
	assert(not RunService:IsRunning(), "A play test is already running - stop it in Studio first.")
	local seconds = math.clamp(tonumber(p.seconds) or 15, 3, 120)
	local mode = (p.mode == "run") and "run" or "play"
	local okService, sts = pcall(function()
		return game:GetService("StudioTestService")
	end)
	if okService and sts then
		local ok, result = pcall(function()
			local testArgs = { agentic = true, seconds = seconds }
			if mode == "run" then
				return sts:ExecuteRunModeAsync(testArgs)
			end
			return sts:ExecutePlayModeAsync(testArgs)
		end)
		assert(ok, "Studio could not run the play test: " .. tostring(result))
		return { ended = true, mode = mode, result = result }, "Play test finished."
	end
	-- Older Studio without StudioTestService: Run mode through RunService only.
	assert(mode == "run", "This Studio version can't start a Play test from a plugin. Use mode 'run', or press Play yourself.")
	RunService:Run()
	task.wait(seconds)
	RunService:Stop()
	return { ended = true, mode = mode, fallback = true }, "Run finished."
end

function handlers.find_instances(p)
	local root = resolve(p.root)
	assert(root, "Root path not found.")
	local needle = p.name and string.lower(p.name) or nil
	local limit = math.clamp(tonumber(p.limit) or 50, 1, 200)
	local items = {}
	local function consider(inst)
		if #items >= limit then
			return
		end
		if needle and not string.find(string.lower(inst.Name), needle, 1, true) then
			return
		end
		if p.className and not inst:IsA(p.className) then
			return
		end
		table.insert(items, { name = inst.Name, className = inst.ClassName, path = pathOf(inst) })
	end
	local containers = { root }
	if root == game then
		containers = {}
		for name in pairs(PROTECTED) do
			local ok, service = pcall(function()
				return game:GetService(name)
			end)
			if ok and service then
				table.insert(containers, service)
			end
		end
	end
	for _, container in ipairs(containers) do
		for _, inst in ipairs(container:GetDescendants()) do
			consider(inst)
			if #items >= limit then
				break
			end
		end
	end
	return items, #items .. " match(es)."
end

function handlers.get_output(p)
	local limit = math.clamp(tonumber(p.limit) or 40, 1, OUTPUT_MAX)
	local items = {}
	for i = #output, 1, -1 do
		local entry = output[i]
		if not p.errorsOnly or entry.kind == "error" then
			table.insert(items, 1, entry)
			if #items >= limit then
				break
			end
		end
	end
	return items, #items .. " output line(s)."
end

local function contextSnapshot()
	local selection = {}
	for i, inst in ipairs(Selection:Get()) do
		if i > 10 then
			break
		end
		table.insert(selection, pathOf(inst))
	end
	local active = activeScript()
	return {
		version = VERSION,
		role = ROLE,
		instance = INSTANCE,
		place = placeName(),
		placeId = game.PlaceId,
		gameId = game.GameId,
		selection = selection,
		activeScript = active and pathOf(active) or nil,
		running = RunService:IsRunning(),
		counts = counts,
	}
end

function handlers.get_context()
	return contextSnapshot(), "ok"
end

-- ---------------------------------------------------------------------------
-- Transport
-- ---------------------------------------------------------------------------
local function request(method, path, body)
	local ok, response = pcall(function()
		return HttpService:RequestAsync({
			Url = BASE_URL .. path,
			Method = method,
			Headers = { ["Content-Type"] = "application/json" },
			Body = body and HttpService:JSONEncode(body) or nil,
		})
	end)
	if not ok or not response.Success then
		return nil
	end
	if response.Body == nil or response.Body == "" then
		return {}
	end
	local decoded, data = pcall(function()
		return HttpService:JSONDecode(response.Body)
	end)
	return decoded and data or {}
end

local enabled = plugin:GetSetting("AgenticBridgeEnabled")
if enabled == nil then
	enabled = false
end

local toolbar = plugin:CreateToolbar("Agentic Workspace")
local button = toolbar:CreateButton("AgenticBridge", "Connect Studio to your local assistant", "", "Bridge")
button.ClickableWhenViewportHidden = true

local generation = 0

local function pollLoop(myGeneration)
	local failures = 0
	while enabled and generation == myGeneration do
		local cmd = request("GET", "/api/roblox/poll?wait=" .. POLL_WAIT .. "&version=" .. VERSION .. "&instance=" .. INSTANCE)
		if cmd == nil then
			failures += 1
			task.wait(math.min(2 * failures, 10)) -- server down: back off quietly
		else
			failures = 0
			if cmd.id and cmd.type then
				local handler = handlers[cmd.type]
				local ok, data, message
				if handler then
					ok, data, message = pcall(handler, cmd.payload or {})
					if not ok then
						message, data = tostring(data), nil
					end
				else
					ok, message = false, "Plugin does not know command '" .. tostring(cmd.type) .. "'. Update the plugin."
				end
				request("POST", "/api/roblox/result", { id = cmd.id, ok = ok, message = message or "", data = data, instance = INSTANCE })
			end
		end
	end
end

local function pushContext()
	local snapshot = contextSnapshot()
	local sending = pendingErrors
	pendingErrors = {}
	snapshot.errors = sending
	if request("POST", "/api/roblox/context", snapshot) == nil then
		-- The assistant is not reachable: keep the errors for the next try (newest 50).
		for _, e in ipairs(pendingErrors) do
			table.insert(sending, e)
		end
		while #sending > 50 do
			table.remove(sending, 1)
		end
		pendingErrors = sending
		return false
	end
	return true
end

local function contextLoop(myGeneration)
	local failures = 0
	while enabled and generation == myGeneration do
		if pushContext() then
			failures = 0
			task.wait(CONTEXT_INTERVAL)
		else
			failures += 1
			task.wait(math.min(CONTEXT_INTERVAL * (failures + 1), 15)) -- back off quietly, then reconnect
		end
	end
end

local function setEnabled(value)
	enabled = value
	plugin:SetSetting("AgenticBridgeEnabled", value)
	button:SetActive(value)
	generation += 1
	if value then
		local myGeneration = generation
		if IS_EDIT then
			task.spawn(pollLoop, myGeneration) -- only the edit DataModel takes commands
		end
		task.spawn(contextLoop, myGeneration)
	end
end

button.Click:Connect(function()
	setEnabled(not enabled)
end)

plugin.Unloading:Connect(function()
	generation += 1
end)

setEnabled(enabled)

-- Play-test server copy: report the last errors before the test closes, and end
-- a play test the assistant started once its time is up.
if not IS_EDIT and RunService:IsServer() then
	pcall(function()
		game:BindToClose(function()
			if enabled then
				pushContext()
			end
		end)
	end)
	local okService, sts = pcall(function()
		return game:GetService("StudioTestService")
	end)
	if okService and sts then
		local okArgs, testArgs = pcall(function()
			return sts:GetTestArgs()
		end)
		if okArgs and type(testArgs) == "table" and testArgs.agentic then
			task.delay(math.clamp(tonumber(testArgs.seconds) or 15, 3, 120), function()
				if enabled then
					pushContext()
				end
				pcall(function()
					sts:EndTest({ errors = counts.errors, warnings = counts.warnings })
				end)
			end)
		end
	end
end
