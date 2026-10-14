// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./MaterialAgentRegistry.sol";

/// @title PredictionRecord
/// @notice C 端核心操作：用户调用某个材料预测 Agent，把"这次调用"写进 BOT Chain
///          （对应《Agent竞马结果上链》方案 · Contract 3 / 方案A）。
///
/// 流程：连接钱包 → 选材料领域与 Agent → 链下得到预测（值 + 置信区间）
///       → 本地算 payloadHash → submitPrediction → 拿到 tx 与区块浏览器链接。
///
/// 红线（写进代码，与 src/fschain.py 一致）：
///   - 链上只存：调用者钱包、Agent id、材料类别、指标名、脱敏定点数、payloadHash；
///   - 不上链：配方组分、真实实测值、供应商/客户/项目名、采购订单；
///   - redacted = true 时所有数值字段强制为 0，合约侧二次校验，防止前端传错；
///   - payloadHash = keccak256(预测单 canonical 描述)，链下重算必须与当时一致 ——
///     事后改本地记录里的预测值，链上这个哈希就对不上。
contract PredictionRecord {
    string public constant VERSION = "mae-prediction/1.0";

    struct Prediction {
        uint256 id;
        address user;          // 发起这笔交易的钱包（真正的 C 端身份）
        uint256 agentId;       // 被调用的 Agent
        bytes32 materialHash;  // keccak256(材料类别 + 标识)
        string  materialClass; // 材料类别，如 "epoxy_adhesive"（≤32 字符，不含配方）
        string  property;      // 指标名，如 "lap_shear_strength"
        int32   valueMilli;    // 预测值 ×1000；redacted 时恒 0
        int32   ciLowMilli;
        int32   ciHighMilli;
        bool    redacted;
        bytes32 payloadHash;   // 预测单 canonical 哈希
        uint64  claimedAt;     // 链下预测产生时间（仅核对）
        uint64  at;            // 链上时间
    }

    MaterialAgentRegistry public registry;

    Prediction[] private _preds;                 // id 从 1 开始
    mapping(address => uint64) public lastPredictAt;
    mapping(address => uint32) public userCount;
    uint32 public userTotal;                     // 去重后的调用钱包数
    uint32 public redactedCount;
    uint32 public cooldown;                      // 同一钱包两次提交的最小间隔（秒）

    address public owner;
    mapping(address => bool) public scribe;
    bool public paused;

    event PredictionSubmitted(uint256 indexed id, address indexed user, uint256 indexed agentId,
                              bytes32 materialHash, string property, int32 valueMilli,
                              int32 ciLowMilli, int32 ciHighMilli, bool redacted, bytes32 payloadHash);
    event CooldownSet(uint32 seconds_);
    event ScribeSet(address indexed scribe, bool allowed);
    event PausedSet(bool paused);

    modifier onlyOwner() { require(msg.sender == owner, "not owner"); _; }
    modifier whenAlive() { require(!paused, "paused"); _; }

    constructor(address registry_, uint32 cooldown_) {
        require(registry_ != address(0), "zero registry");
        registry = MaterialAgentRegistry(registry_);
        cooldown = cooldown_;
        owner = msg.sender;
        scribe[msg.sender] = true;
        _preds.push();                  // id 0 占位：保证真实 prediction id 从 1 开始，predictionCount() 空仓为 0 不下溢
        emit CooldownSet(cooldown_);
        emit ScribeSet(msg.sender, true);
    }

    function setScribe(address a, bool allowed) external onlyOwner {
        scribe[a] = allowed;
        emit ScribeSet(a, allowed);
    }
    function setPaused(bool p) external onlyOwner { paused = p; emit PausedSet(p); }
    function setCooldown(uint32 seconds_) external onlyOwner {
        cooldown = seconds_;
        emit CooldownSet(seconds_);
    }

    // ------------------------------------------------------------------ C 端核心写操作
    /// @notice 提交一次预测调用记录。同一 wallet 受 cooldown 限制（防刷，可由 owner 调低）。
    function submitPrediction(
        uint256 agentId,
        bytes32 materialHash,
        string calldata materialClass,
        string calldata property,
        int32 valueMilli, int32 ciLowMilli, int32 ciHighMilli,
        bool redacted, bytes32 payloadHash, uint64 claimedAt
    ) external whenAlive returns (uint256 id) {
        require(registry.exists(agentId), "no such agent");
        require(registry.getAgent(agentId).active, "agent retired");
        require(materialHash != bytes32(0), "zero materialHash");
        require(bytes(materialClass).length > 0 && bytes(materialClass).length <= 32, "bad materialClass");
        require(bytes(property).length > 0 && bytes(property).length <= 64, "bad property");
        require(payloadHash != bytes32(0), "zero payloadHash");
        require(ciLowMilli <= valueMilli && valueMilli <= ciHighMilli, "ci must bracket value");
        // 红线硬约束（链上二次校验）：脱敏时不许夹带任何数值
        if (redacted) {
            require(valueMilli == 0 && ciLowMilli == 0 && ciHighMilli == 0,
                    "redacted must blank numbers");
            redactedCount += 1;
        }
        if (cooldown > 0) {
            require(block.timestamp >= lastPredictAt[msg.sender] + cooldown, "cooldown");
            lastPredictAt[msg.sender] = uint64(block.timestamp);
        }

        _preds.push(Prediction({
            id: _preds.length, user: msg.sender, agentId: agentId, materialHash: materialHash,
            materialClass: materialClass, property: property,
            valueMilli: valueMilli, ciLowMilli: ciLowMilli, ciHighMilli: ciHighMilli,
            redacted: redacted, payloadHash: payloadHash, claimedAt: claimedAt,
            at: uint64(block.timestamp)
        }));
        id = _preds.length - 1;
        if (userCount[msg.sender] == 0) userTotal += 1;
        userCount[msg.sender] += 1;
        emit PredictionSubmitted(id, msg.sender, agentId, materialHash, property,
                                 valueMilli, ciLowMilli, ciHighMilli, redacted, payloadHash);
    }

    // ------------------------------------------------------------------ 读
    function predictionCount() external view returns (uint256) { return _preds.length - 1; }

    function getPrediction(uint256 id) external view returns (Prediction memory) {
        require(id >= 1 && id < _preds.length, "no such prediction");
        return _preds[id];
    }

    /// @notice 批量取最近 N 条（前端交易记录表一次拿全，最新在前）。
    function latest(uint256 n) external view returns (Prediction[] memory out) {
        uint256 len = _preds.length - 1;
        if (n > len) n = len;
        out = new Prediction[](n);
        for (uint256 i = 0; i < n; i++) out[i] = _preds[len - i];
    }

    function stats() external view returns (uint256 total, uint32 redacted_, uint32 users) {
        return (_preds.length - 1, redactedCount, userTotal);
    }
}
