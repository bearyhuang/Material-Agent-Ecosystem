// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title FormulaSwarmAttestor
/// @notice 本合约只服务两件事（对应 ai4s04 §4 必做档 #4 / #5）：
///   ① 人审批准留痕（evo.experiment_plan）
///      —— 谁（哈希）、在什么时刻、批准了投放哪一组实验：事后不可否认，也不可补签。
///   ② 预测先行登记锁（evo.prediction_register）
///      —— 登记一次即锁死；事后改预测值让对齐率变好看，就会对不上链上那个哈希。
/// @dev 红线（写进代码，不写在 PPT）：
///   - 链上只有哈希与脱敏定点数，没有配方组分、没有真实实测值、没有供应商/客户/项目名、没有采购订单。
///   - 批准人姓名只以 keccak256(姓名) 上链：可核对、不泄露。
///   - redacted=true 时所有数值字段强制为 0，合约侧二次校验，防止后端传错。
contract FormulaSwarmAttestor {
    string public constant VERSION = "fs-attestor/1.0";

    // ------------------------------------------------------------------ 角色
    address public owner;
    mapping(address => bool) public scribes;
    bool public paused;

    // ------------------------------------------------------------------ 结构
    struct Approval {
        bytes32 planHash;      // 链下 keccak256，见 src/fschain.py: plan_hash()
        bytes32 approverHash;  // keccak256(批准人姓名)
        uint32  plansCount;    // 本次批准涉及的计划条数
        uint64  claimedAt;     // 链下 approved_at（unix 秒），供核对
        uint64  at;            // 链上 block.timestamp —— 真正的不可抵赖时间
        address scribe;        // 写链后端地址
    }

    struct Prediction {
        bytes32 regHash;       // 链下 keccak256，见 src/fschain.py: prediction_hash()
        string  experimentNo;
        int32   predictedMilli;// 定点 ×1000；redacted 时恒为 0
        int32   ciLowMilli;
        int32   ciHighMilli;
        bool    redacted;      // true = 链下是真实数据，数值已抹零
        uint64  claimedAt;     // 链下 registered_at
        uint64  at;            // 链上登记时间
        address scribe;
    }

    /// @dev 批量登记入参（避免 N 条预测占 N 笔交易）
    struct PredInput {
        bytes32 regHash;
        string  experimentNo;
        int32   predictedMilli;
        int32   ciLowMilli;
        int32   ciHighMilli;
        bool    redacted;
        uint64  claimedAt;
    }

    // ------------------------------------------------------------------ 存储
    mapping(bytes32 => Approval)   private _approvals;
    mapping(bytes32 => Prediction) private _predictions;
    uint256 public approvalCount;
    uint256 public predictionCount;

    // ------------------------------------------------------------------ 事件
    event ApprovalAttested(bytes32 indexed planHash, bytes32 indexed approverHash,
                           uint32 plansCount, uint64 claimedAt, uint64 at, address indexed scribe);
    event PredictionRegistered(bytes32 indexed regHash, string experimentNo,
                               int32 predictedMilli, int32 ciLowMilli, int32 ciHighMilli,
                               bool redacted, uint64 claimedAt, uint64 at, address indexed scribe);
    event ScribeSet(address indexed scribe, bool allowed);
    event PausedSet(bool paused);

    // ------------------------------------------------------------------ 修饰
    modifier onlyOwner()  { require(msg.sender == owner, "not owner"); _; }
    modifier onlyScribe() { require(scribes[msg.sender] || msg.sender == owner, "not scribe"); _; }
    modifier whenAlive()  { require(!paused, "paused"); _; }

    constructor() {
        owner = msg.sender;
        scribes[msg.sender] = true;
        emit ScribeSet(msg.sender, true);
    }

    function setScribe(address a, bool allowed) external onlyOwner {
        scribes[a] = allowed;
        emit ScribeSet(a, allowed);
    }

    function setPaused(bool p) external onlyOwner { paused = p; emit PausedSet(p); }

    function transferOwnership(address a) external onlyOwner {
        require(a != address(0), "zero");
        owner = a;
    }

    // ------------------------------------------------- ① 人审批准留痕
    /// @notice 锚定一次人工批准。同一 planHash 只能登记一次 —— 防"事后补签并存多版本"。
    function attestApproval(bytes32 planHash, bytes32 approverHash, uint32 plansCount, uint64 claimedAt)
        external onlyScribe whenAlive
    {
        _attestApproval(planHash, approverHash, plansCount, claimedAt);
    }

    /// @notice 批量锚定（一次交易锚 N 条，逐条 emit，已存在的跳过 —— 可重复运行）。
    /// @return n 本次新增条数
    function attestApprovalBatch(bytes32[] calldata planHashes, bytes32 approverHash, uint64 claimedAt)
        external onlyScribe whenAlive returns (uint32 n)
    {
        require(planHashes.length > 0 && planHashes.length <= 200, "bad length");
        require(approverHash != bytes32(0), "zero approverHash");
        uint32 added = 0;
        for (uint256 i = 0; i < planHashes.length; i++) {
            if (planHashes[i] == bytes32(0)) continue;
            if (_approvals[planHashes[i]].at != 0) continue;   // 幂等：已锚过则跳过
            _writeApproval(planHashes[i], approverHash, uint32(planHashes.length), claimedAt);
            added += 1;
        }
        require(added > 0, "nothing new");
        return added;
    }

    function _attestApproval(bytes32 planHash, bytes32 approverHash, uint32 plansCount, uint64 claimedAt) private {
        require(planHash != bytes32(0), "zero planHash");
        require(approverHash != bytes32(0), "zero approverHash");
        require(_approvals[planHash].at == 0, "approval already attested");
        _writeApproval(planHash, approverHash, plansCount, claimedAt);
    }

    function _writeApproval(bytes32 planHash, bytes32 approverHash, uint32 plansCount, uint64 claimedAt) private {
        uint64 nowTs = uint64(block.timestamp);
        _approvals[planHash] = Approval({
            planHash: planHash, approverHash: approverHash, plansCount: plansCount,
            claimedAt: claimedAt, at: nowTs, scribe: msg.sender
        });
        approvalCount += 1;
        emit ApprovalAttested(planHash, approverHash, plansCount, claimedAt, nowTs, msg.sender);
    }

    // ------------------------------------------------- ② 预测先行登记锁
    /// @notice 登记一次即锁死。事后改数据库里的预测值，链上这个哈希对不上。
    function registerPrediction(
        bytes32 regHash, string calldata experimentNo,
        int32 predictedMilli, int32 ciLowMilli, int32 ciHighMilli,
        bool redacted, uint64 claimedAt
    ) external onlyScribe whenAlive {
        _registerPrediction(PredInput({
            regHash: regHash, experimentNo: experimentNo,
            predictedMilli: predictedMilli, ciLowMilli: ciLowMilli, ciHighMilli: ciHighMilli,
            redacted: redacted, claimedAt: claimedAt
        }));
    }

    /// @notice 批量登记（逐条 emit，已存在的跳过）。
    function registerPredictionBatch(PredInput[] calldata items)
        external onlyScribe whenAlive returns (uint32 n)
    {
        require(items.length > 0 && items.length <= 200, "bad length");
        uint32 added = 0;
        for (uint256 i = 0; i < items.length; i++) {
            if (items[i].regHash == bytes32(0)) continue;
            if (_predictions[items[i].regHash].at != 0) continue;
            _writePrediction(items[i]);
            added += 1;
        }
        require(added > 0, "nothing new");
        return added;
    }

    function _registerPrediction(PredInput memory p) private {
        require(p.regHash != bytes32(0), "zero regHash");
        require(_predictions[p.regHash].at == 0, "prediction already registered");
        require(bytes(p.experimentNo).length > 0 && bytes(p.experimentNo).length <= 64, "bad experimentNo");
        // 红线硬约束（链上二次校验）：脱敏时不许夹带任何数值
        if (p.redacted) {
            require(p.predictedMilli == 0 && p.ciLowMilli == 0 && p.ciHighMilli == 0,
                    "redacted must blank numbers");
        }
        _writePrediction(p);
    }

    function _writePrediction(PredInput memory p) private {
        uint64 nowTs = uint64(block.timestamp);
        _predictions[p.regHash] = Prediction({
            regHash: p.regHash, experimentNo: p.experimentNo,
            predictedMilli: p.predictedMilli, ciLowMilli: p.ciLowMilli, ciHighMilli: p.ciHighMilli,
            redacted: p.redacted, claimedAt: p.claimedAt, at: nowTs, scribe: msg.sender
        });
        predictionCount += 1;
        emit PredictionRegistered(p.regHash, p.experimentNo, p.predictedMilli, p.ciLowMilli,
                                  p.ciHighMilli, p.redacted, p.claimedAt, nowTs, msg.sender);
    }

    // ------------------------------------------------------------------ 读
    function approvalExists(bytes32 planHash) external view returns (bool) {
        return _approvals[planHash].at != 0;
    }

    function getApproval(bytes32 planHash) external view returns (Approval memory) {
        return _approvals[planHash];
    }

    function predictionExists(bytes32 regHash) external view returns (bool) {
        return _predictions[regHash].at != 0;
    }

    function getPrediction(bytes32 regHash) external view returns (Prediction memory) {
        return _predictions[regHash];
    }

    function stats() external view returns (uint256 approvals, uint256 predictions) {
        return (approvalCount, predictionCount);
    }
}
