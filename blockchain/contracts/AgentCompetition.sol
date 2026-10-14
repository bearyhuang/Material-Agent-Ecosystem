// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./MaterialAgentRegistry.sol";

/// @title AgentCompetition
/// @notice Agent 竞马：谁在哪个材料任务上赢了（对应《Agent竞马结果上链》方案 · Contract 2 / 方案B）。
///
/// 口径（与项目真实数据一致，见 /api/runs 的 dev_mae / holdout_mae）：
///   scoreMilli = 指标 × 1000，**越小越好**（MAE / RMSE 口径）。
///   例：holdout_mae = 0.412 → scoreMilli = 412。
///
/// 链上只存哈希与脱敏定点数：
///   - scoreHash = keccak256(该次竞马的 canonical 成绩单)，含 dev/golden MAE、覆盖率、
///     轮次、切分、试验数等全部决策要素；链下重算必须与当时一致。
///   - redacted = true 时 scoreMilli 强制为 0（真实成绩在链下），合约侧二次校验。
///
/// 谁能提交排名：Agent owner / 被委托钱包（delegate）/ scribe —— 由 MaterialAgentRegistry 判定，
/// 因此"审核员用自己钱包推一笔真实的排名交易"是被授权的、可审计的，而不是匿名刷分。
contract AgentCompetition {
    string public constant VERSION = "mae-competition/1.0";

    struct Race {
        uint256 id;
        bytes32 raceHash;      // keccak256(竞马配置 canonical 描述)
        string  materialTask;  // 如 "lap_shear_strength" / "capacity"
        string  domain;        // 如 "adhesive" / "battery"
        address creator;       // 开赛人
        uint64  at;
        bool    finalized;
        uint256 winnerAgentId;
        uint32  participants;  // 已提交排名的 Agent 数
    }

    struct Ranking {
        uint256 agentId;
        int32   scoreMilli;    // 越小越好；redacted 时恒 0
        bytes32 scoreHash;     // 成绩单 canonical 哈希
        bool    redacted;
        address endorser;      // 实际提交这笔交易的钱包
        uint64  claimedAt;     // 链下成绩产生时间（unix 秒，仅核对）
        uint64  at;            // 链上时间
        uint32  revision;      // 第几次提交（0 起）
    }

    MaterialAgentRegistry public registry;

    Race[] private _races;                                   // id 从 1 开始
    mapping(uint256 => mapping(uint256 => Ranking)) private _rank;   // raceId => agentId => 最新
    mapping(uint256 => uint256[]) private _raceAgents;               // raceId => agentId[]
    mapping(uint256 => int32) public bestMilli;                      // agentId => 历史最好（越小越好）
    mapping(uint256 => uint32) public bestRevision;                  // agentId => 该成绩第几次提交

    address public owner;
    mapping(address => bool) public scribe;
    bool public paused;

    event RaceOpened(uint256 indexed id, bytes32 raceHash, string materialTask,
                     string domain, address indexed creator);
    event RankingSubmitted(uint256 indexed raceId, uint256 indexed agentId, int32 scoreMilli,
                           bytes32 scoreHash, bool redacted, address indexed endorser, uint32 revision);
    event RaceFinalized(uint256 indexed raceId, uint256 indexed winnerAgentId,
                        int32 scoreMilli, address indexed by);
    event ScribeSet(address indexed scribe, bool allowed);
    event PausedSet(bool paused);

    modifier onlyOwner()  { require(msg.sender == owner, "not owner"); _; }
    modifier onlyScribe() { require(scribe[msg.sender] || msg.sender == owner, "not scribe"); _; }
    modifier whenAlive()  { require(!paused, "paused"); _; }

    constructor(address registry_) {
        require(registry_ != address(0), "zero registry");
        registry = MaterialAgentRegistry(registry_);
        owner = msg.sender;
        scribe[msg.sender] = true;
        _races.push();                  // id 0 占位：保证真实 race id 从 1 开始，raceCount() 空仓为 0 不下溢
        emit ScribeSet(msg.sender, true);
    }

    function setScribe(address a, bool allowed) external onlyOwner {
        scribe[a] = allowed;
        emit ScribeSet(a, allowed);
    }
    function setPaused(bool p) external onlyOwner { paused = p; emit PausedSet(p); }

    // ------------------------------------------------------------------ 开赛
    /// @notice 任何人可开一场竞马。raceHash 覆盖"赛什么、怎么评"的全部要素。
    function openRace(bytes32 raceHash, string calldata materialTask, string calldata domain)
        external whenAlive returns (uint256 id)
    {
        require(raceHash != bytes32(0), "zero raceHash");
        require(bytes(materialTask).length > 0 && bytes(materialTask).length <= 64, "bad task");
        require(bytes(domain).length > 0 && bytes(domain).length <= 32, "bad domain");
        _races.push(Race({
            id: _races.length, raceHash: raceHash, materialTask: materialTask,
            domain: domain, creator: msg.sender, at: uint64(block.timestamp),
            finalized: false, winnerAgentId: 0, participants: 0
        }));
        id = _races.length - 1;
        emit RaceOpened(id, raceHash, materialTask, domain, msg.sender);
    }

    // ------------------------------------------------------------------ 提交排名（C 端核心操作）
    /// @notice 给某 Agent 在一场竞马里提交成绩。授权判定走 Registry.canRank()。
    function submitRanking(uint256 raceId, uint256 agentId, int32 scoreMilli,
                           bytes32 scoreHash, bool redacted, uint64 claimedAt)
        external whenAlive
    {
        _requireRace(raceId);
        Race storage r = _races[raceId];
        require(!r.finalized, "race finalized");
        require(registry.exists(agentId), "no such agent");
        require(registry.canRank(agentId, msg.sender), "not authorized to rank");
        require(scoreHash != bytes32(0), "zero scoreHash");
        // 红线硬约束（链上二次校验）：脱敏时不许夹带任何数值
        if (redacted) require(scoreMilli == 0, "redacted must blank numbers");

        Ranking storage k = _rank[raceId][agentId];
        if (k.at == 0) {
            _raceAgents[raceId].push(agentId);
            r.participants += 1;
        }
        k.agentId = agentId;
        k.scoreMilli = scoreMilli;
        k.scoreHash = scoreHash;
        k.redacted = redacted;
        k.endorser = msg.sender;
        k.claimedAt = claimedAt;
        k.at = uint64(block.timestamp);
        k.revision += 1;

        // 历史最好成绩（只认非脱敏提交，防止脱敏 0 值污染）
        if (!redacted && (bestRevision[agentId] == 0 || scoreMilli < bestMilli[agentId])) {
            bestMilli[agentId] = scoreMilli;
            bestRevision[agentId] = k.revision;
        }
        emit RankingSubmitted(raceId, agentId, scoreMilli, scoreHash, redacted, msg.sender, k.revision);
    }

    // ------------------------------------------------------------------ 定冠军
    /// @notice 开赛人或 scribe 定冠军：链上留"谁在什么赛上赢了"。
    function finalizeRace(uint256 raceId, uint256 winnerAgentId) external whenAlive {
        _requireRace(raceId);
        Race storage r = _races[raceId];
        require(!r.finalized, "already finalized");
        require(msg.sender == r.creator || scribe[msg.sender] || msg.sender == owner, "not race creator");
        require(registry.exists(winnerAgentId), "no such agent");
        Ranking storage w = _rank[raceId][winnerAgentId];
        require(w.at != 0, "winner has no ranking");
        r.finalized = true;
        r.winnerAgentId = winnerAgentId;
        emit RaceFinalized(raceId, winnerAgentId, w.scoreMilli, msg.sender);
    }

    // ------------------------------------------------------------------ 读
    function raceCount() external view returns (uint256) { return _races.length - 1; }

    function getRace(uint256 id) external view returns (Race memory) {
        _requireRace(id);
        return _races[id];
    }

    function getRanking(uint256 raceId, uint256 agentId) external view returns (Ranking memory) {
        _requireRace(raceId);
        return _rank[raceId][agentId];
    }

    function raceAgents(uint256 raceId) external view returns (uint256[] memory) {
        _requireRace(raceId);
        return _raceAgents[raceId];
    }

    /// @notice 一场竞马的完整榜单（前端一次拿全）。
    function getRaceRankings(uint256 raceId, uint256[] calldata agentIds)
        external view returns (Ranking[] memory out)
    {
        _requireRace(raceId);
        out = new Ranking[](agentIds.length);
        for (uint256 i = 0; i < agentIds.length; i++) out[i] = _rank[raceId][agentIds[i]];
    }

    function stats() external view returns (uint256 races, uint256 finalized, uint32 participantsMax) {
        races = _races.length - 1;
        for (uint256 i = 1; i < _races.length; i++) {
            if (_races[i].finalized) finalized += 1;
            if (_races[i].participants > participantsMax) participantsMax = _races[i].participants;
        }
    }

    function _requireRace(uint256 id) private view {
        require(id >= 1 && id < _races.length, "no such race");
    }
}
