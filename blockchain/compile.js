#!/usr/bin/env node
/**
 * 用 solc-js 离线编译 contracts/*.sol，产出 abi/bytecode 到 abi/*.json
 *
 *   node blockchain/compile.js              # 编译全部
 *   node blockchain/compile.js --check      # 只做编译校验，不落盘
 */
const fs = require('fs');
const path = require('path');
const solc = require('solc');

const ROOT = __dirname;                       // blockchain/
const SRC = path.join(ROOT, 'contracts');
const OUT = path.join(ROOT, 'abi');

const checkOnly = process.argv.includes('--check');
if (!checkOnly) fs.mkdirSync(OUT, { recursive: true });

const files = fs.readdirSync(SRC).filter((f) => f.endsWith('.sol'));
if (!files.length) {
  console.error('no .sol found in chain/contracts');
  process.exit(1);
}

const sources = {};
for (const f of files) {
  sources[f] = { content: fs.readFileSync(path.join(SRC, f), 'utf8') };
}

const input = {
  language: 'Solidity',
  sources,
  settings: {
    optimizer: { enabled: true, runs: 200 },
    evmVersion: 'paris',
    outputSelection: { '*': { '*': ['abi', 'evm.bytecode.object', 'evm.deployedBytecode.object'] } },
  },
};

const out = JSON.parse(solc.compile(JSON.stringify(input)));
const errors = (out.errors || []).filter((e) => e.severity === 'error');
for (const e of out.errors || []) {
  if (e.severity === 'error') console.error('[error]', e.formattedMessage);
  else if (!/SPDX|License/.test(e.message)) console.warn('[warn]', e.formattedMessage);
}
if (errors.length) process.exit(2);

console.log('solc', solc.version());
for (const file of Object.keys(out.contracts || {})) {
  for (const name of Object.keys(out.contracts[file])) {
    const c = out.contracts[file][name];
    const bytecode = '0x' + c.evm.bytecode.object;
    const deployed = '0x' + c.evm.deployedBytecode.object;
    if (checkOnly) {
      console.log(`  OK ${name}  bytecode ${bytecode.length} chars`);
      continue;
    }
    const target = path.join(OUT, `${name}.json`);
    fs.writeFileSync(
      target,
      JSON.stringify({ contractName: name, abi: c.abi, bytecode, deployedBytecode: deployed,
                       source: file, solc: solc.version() }, null, 2)
    );
    console.log(`  -> ${path.relative(ROOT, target)}  (abi ${c.abi.length} fns, bytecode ${(bytecode.length - 2) / 2} bytes)`);
  }
}
