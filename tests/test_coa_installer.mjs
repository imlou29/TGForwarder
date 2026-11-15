import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const installer = fileURLToPath(new URL('../openclaw/install_catalog.mjs', import.meta.url));
const helper = fileURLToPath(new URL('../openclaw/coa_lookup.mjs', import.meta.url));
const question = '* "Is there a COA?" / "COA for X?" → if the GB is missing, ask which GB and STOP. If the GB is known, use the live COA lookup; return the matching link(s), purity and quantity when available, plus the required live-page confirmation reminder. STOP.';

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'coa-install-test-'));
  const skill = path.join(root, 'skills/tegridy-coa-lookup');
  fs.mkdirSync(path.join(skill, 'scripts'), {recursive:true});
  fs.writeFileSync(path.join(skill, 'SKILL.md'), '---\nname: tegridy-coa-lookup\n---\nOLD WEBSITE WORKFLOW');
  fs.writeFileSync(path.join(skill, 'scripts/coa-lookup.mjs'), 'OLD HELPER');
  const original = 'OTHER RULES\nLive COA Lookup Override\nOLD COA RULES\n4. GENERAL KNOWLEDGE IS ALLOWED\nKEEP THIS\n' + question + '\nOTHER SECURITY RULES\n';
  fs.writeFileSync(path.join(root, 'AGENTS.md'), original);
  return {root, skill, original};
}

test('replaces old workflow, preserves other rules and backs up all three originals', () => {
  const f = fixture();
  try {
    const result = spawnSync(process.execPath, [installer], {encoding:'utf8', env:{...process.env, OPENCLAW_WORKSPACE_DIR:f.root}});
    assert.equal(result.status, 0, result.stderr);
    const agents = fs.readFileSync(path.join(f.root, 'AGENTS.md'), 'utf8');
    assert.ok(agents.includes('OTHER SECURITY RULES'));
    assert.ok(agents.includes('KEEP THIS'));
    assert.ok(agents.includes('Telegram COA Lookup Override'));
    assert.ok(!agents.includes('OLD COA RULES'));
    assert.ok(!agents.includes('plus the required live-page confirmation reminder'));
    const backup = path.join(f.root, 'backups', fs.readdirSync(path.join(f.root, 'backups'))[0]);
    assert.equal(fs.readFileSync(path.join(backup, 'AGENTS.md'), 'utf8'), f.original);
    assert.equal(JSON.parse(fs.readFileSync(path.join(backup, 'manifest.json'))).length, 3);
    assert.ok(fs.readFileSync(path.join(f.skill, 'SKILL.md'), 'utf8').includes('# Telegram COA Lookup'));
    assert.equal(fs.readFileSync(path.join(f.skill, 'scripts/coa-lookup.mjs'), 'utf8'), fs.readFileSync(helper, 'utf8'));
    const repeated = spawnSync(process.execPath, [installer], {encoding:'utf8', env:{...process.env, OPENCLAW_WORKSPACE_DIR:f.root}});
    assert.notEqual(repeated.status, 0);
    assert.equal(fs.readFileSync(path.join(f.root, 'AGENTS.md'), 'utf8'), agents);
  } finally { fs.rmSync(f.root, {recursive:true, force:true}); }
});

test('unknown AGENTS format fails before changing any active file', () => {
  const f = fixture();
  try {
    fs.writeFileSync(path.join(f.root, 'AGENTS.md'), 'Unexpected format');
    const result = spawnSync(process.execPath, [installer], {encoding:'utf8', env:{...process.env, OPENCLAW_WORKSPACE_DIR:f.root}});
    assert.notEqual(result.status, 0);
    assert.equal(fs.readFileSync(path.join(f.skill, 'scripts/coa-lookup.mjs'), 'utf8'), 'OLD HELPER');
    assert.ok(!fs.existsSync(path.join(f.root, 'backups')));
  } finally { fs.rmSync(f.root, {recursive:true, force:true}); }
});

test('lookup with missing GB stops before any connection attempt', () => {
  const result = spawnSync(process.execPath, [helper, '--query', 'RT10'], {encoding:'utf8', env:{...process.env, COA_API_TOKEN:'', COA_CATALOG_URL:''}});
  assert.equal(result.status, 0);
  assert.equal(JSON.parse(result.stdout).status, 'needs_gb');
});
