// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title MaterialAgentRegistry
/// @notice 材料预测 Agent 的链上身份证（对应《Agent竞马结果上链》方案 · Contract 1）。
///
/// 每个 Agent 是一个"算法组合体"：{特征处理, 算法, 超参数, 训练策略}。
/// 链上只存**身份与能力证明**，不存模型本体、不存训练数据：
///   - modelHash   = keccak256(算法组合的 canonical 描述)，换任何一项超参即失配
///   - domain      = 适用材料领域（battery / alloy / semiconductor / adhesive …）
///   - version     = 模型版本号（人类可读，如 "ridge-v3"）
///
/// 红线（与 src/fschain.py 一致，写进代码）：
///   - 链上不出现配方组分、实测值、供应商/客户/项目名、采购订单；
///   - 任何人（含审核员钱包）都可注册自己的 Agent，msg.sender 即 owner；
///   - 排名提交权可委托（delegate），用于"审核员用自己钱包推排名交易"的可审计场景。
contract MaterialAgentRegistry {
    string public constant VERSION = "mae-registry/1.0";

    struct Agent {
        uint256 id;
        string  name;        // 人类可读名，如 "champion_ridge"
        string  domain;      // 适用领域，如 "adhesive" / "battery"
        bytes32 modelHash;   // keccak256(算法组合 canonical 描述)，不得为 0
        string  version;     // 模型版本
        address owner;       // 注册人钱包
        uint64  at;          // 注册时间（block.timestamp）
        bool    active;      // false = 已退役，不参与新竞马
    }

    Agent[] private _agents;                       // id 从 1 开始（_agents[0] 占位）
    mapping(address => uint256[]) private _byOwner;
    mapping(uint256 => mapping(address => bool)) public delegate;  // agentId => 钱包 => 可代提交排名

    // 后端/官方写入口（与 FormulaSwarmAttestor 的 scribe 同思路：只用于批量播种）
    address public owner;
    mapping(address => bool) public scribe;
    bool public paused;

    event AgentRegistered(uint256 indexed id, string name, string domain,
                          bytes32 modelHash, string version, address indexed owner);
    event AgentActiveSet(uint256 indexed id, bool active);
    event DelegateSet(uint256 indexed id, address indexed who, bool allowed);
    event ScribeSet(address indexed scribe, bool allowed);
    event PausedSet(bool paused);
    event OwnershipTransferred(address indexed from, address indexed to);

    modifier onlyOwner()  { require(msg.sender == owner, "not owner"); _; }
    modifier onlyScribe() { require(scribe[msg.sender] || msg.sender == owner, "not scribe"); _; }
    modifier whenAlive()  { require(!paused, "paused"); _; }

    constructor() {
        owner = msg.sender;
        scribe[msg.sender] = true;
        _agents.push();                 // id 0 占位：保证真实 Agent id 从 1 开始，agentCount() 空仓为 0 不下溢
        emit ScribeSet(msg.sender, true);
    }

    // ------------------------------------------------------------------ 管理
    function setScribe(address a, bool allowed) external onlyOwner {
        scribe[a] = allowed;
        emit ScribeSet(a, allowed);
    }
    function setPaused(bool p) external onlyOwner { paused = p; emit PausedSet(p); }
    function transferOwnership(address a) external onlyOwner {
        require(a != address(0), "zero");
        emit OwnershipTransferred(owner, a);
        owner = a;
    }

    // ------------------------------------------------------------------ 注册（C 端可调用）
    struct AgentInput {
        string  name;
        string  domain;
        bytes32 modelHash;
        string  version;
    }

    /// @notice 任何人可用自己的钱包注册一个 Agent。返回新 Agent id（从 1 开始）。
    function registerAgent(string calldata name, string calldata domain,
                           bytes32 modelHash, string calldata version)
        external whenAlive returns (uint256 id)
    {
        return _register(name, domain, modelHash, version, msg.sender);
    }

    /// @notice 批量播种（scribe/owner）：一次交易注册 N 个 Agent，逐条 emit。
    function registerAgentBatch(AgentInput[] calldata items)
        external onlyScribe whenAlive returns (uint32 n)
    {
        require(items.length > 0 && items.length <= 100, "bad length");
        for (uint256 i = 0; i < items.length; i++) {
            _register(items[i].name, items[i].domain, items[i].modelHash,
                      items[i].version, msg.sender);
            n += 1;
        }
    }

    function _register(string calldata name, string calldata domain,
                       bytes32 modelHash, string calldata version, address who) private returns (uint256) {
        require(bytes(name).length > 0 && bytes(name).length <= 64, "bad name");
        require(bytes(domain).length > 0 && bytes(domain).length <= 32, "bad domain");
        require(bytes(version).length <= 32, "bad version");
        require(modelHash != bytes32(0), "zero modelHash");   // 必须有算法组合证明
        _agents.push(Agent({
            id: _agents.length, name: name, domain: domain, modelHash: modelHash,
            version: version, owner: who, at: uint64(block.timestamp), active: true
        }));
        uint256 id = _agents.length - 1;
        _byOwner[who].push(id);
        emit AgentRegistered(id, name, domain, modelHash, version, who);
        return id;
    }

    // ------------------------------------------------------------------ 退役 / 委托
    function setActive(uint256 id, bool active) external {
        _requireAgent(id);
        require(msg.sender == owner || msg.sender == _agents[id].owner, "not agent owner");
        _agents[id].active = active;
        emit AgentActiveSet(id, active);
    }

    /// @notice Agent owner 授权某钱包代提交该 Agent 的排名（审核员钱包用）。
    function setDelegate(uint256 id, address who, bool allowed) external {
        _requireAgent(id);
        require(msg.sender == _agents[id].owner, "not agent owner");
        delegate[id][who] = allowed;
        emit DelegateSet(id, who, allowed);
    }

    // ------------------------------------------------------------------ 读
    function agentCount() external view returns (uint256) { return _agents.length - 1; }

    function getAgent(uint256 id) external view returns (Agent memory) {
        _requireAgent(id);
        return _agents[id];
    }

    function exists(uint256 id) external view returns (bool) {
        return id >= 1 && id < _agents.length;
    }

    function canRank(uint256 id, address who) external view returns (bool) {
        if (id < 1 || id >= _agents.length) return false;
        if (scribe[who] || who == owner) return true;
        return _agents[id].owner == who || delegate[id][who];
    }

    function agentsOf(address who) external view returns (uint256[] memory) { return _byOwner[who]; }

    /// @notice 批量取 Agent（前端排行榜一次拿全，避免 N 次 RPC）。
    function getAgents(uint256[] calldata ids) external view returns (Agent[] memory out) {
        out = new Agent[](ids.length);
        for (uint256 i = 0; i < ids.length; i++) {
            _requireAgent(ids[i]);
            out[i] = _agents[ids[i]];
        }
    }

    function _requireAgent(uint256 id) private view {
        require(id >= 1 && id < _agents.length, "no such agent");
    }
}
