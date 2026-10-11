import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile, mkdtemp, writeFile, mkdir, access } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { targetSpec, isCurrent, safePath, npmArguments, checkPlugin, install } from '../scripts/dsh-npm.mjs'
const lock = JSON.parse(await readFile(new URL('../config/dsh/runtime.lock.json', import.meta.url), 'utf8'))
const contract = JSON.parse(await readFile(new URL('./fixtures/dsh-adapter-cases.json', import.meta.url), 'utf8'))
test('与原生端和后端共用兼容判定场景', () => {
  const base = targetSpec(lock)
  for (const scenario of contract.cases) assert.equal(isCurrent({ ...base, ...scenario.installed }, { ...base, ...scenario.target }), scenario.matches, scenario.name)
})

test('版本和入口来自配置，升级及回退均要求精确匹配', () => {
  const target = targetSpec(lock)
  for (const version of ['1.0.0', '0.1.0', '0.2.1-alpha.1']) {
    const next = targetSpec({ ...lock, version })
    assert.equal(next.version, version)
    assert.equal(isCurrent(target, next), false)
    assert.equal(isCurrent(next, next), true)
  }
  assert.equal(isCurrent({ ...target, adapter: 'old' }, target), false)
  assert.equal(isCurrent({ ...target, required_files: [] }, target), false)
})
test('拒绝浮动版本和越界路径', () => {
  for (const version of ['latest', '^0.2.0', 'next']) assert.throws(() => targetSpec({ ...lock, version }))
  for (const relative of ['../escape', 'a/../b', '/absolute', 'a\\b']) assert.throws(() => safePath(tmpdir(), relative))
})
test('安装使用工作区且不维护上游依赖锁文件', () => {
  const args = npmArguments('D:/中文 路径', targetSpec(lock))
  assert.ok(args.includes('--package-lock=false'))
  assert.ok(args.includes('D:/中文 路径'))
  assert.ok(!args.includes('-g'))
  assert.ok(args.includes('--prefer-offline'))
  assert.ok(args.includes('--no-update-notifier'))
  assert.ok(npmArguments('target', targetSpec(lock), '11.17.0').includes('--dangerously-allow-all-scripts'))
  assert.ok(!npmArguments('target', targetSpec(lock), '10.0.0').includes('--dangerously-allow-all-scripts'))
})
test('未适配插件阻止安装', async () => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-plugin-contract-'))
  const spec = targetSpec(lock)
  const record = safePath(root, spec.plugin_record)
  await mkdir(join(record, '..'), { recursive: true })
  await writeFile(record, JSON.stringify({ dsh_version: 'unadapted' }))
  await assert.rejects(checkPlugin(root, spec), /尚未适配/u)
})
test('安装适配器调用精确依赖、生成入口并接入私有 pnpm', async () => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-install-contract-'))
  const program = join(root, 'app'), target = join(root, 'runtime')
  const spec = targetSpec(lock)
  await mkdir(join(program, 'config/dsh'), { recursive: true })
  await writeFile(join(program, 'config/dsh/runtime.lock.json'), JSON.stringify(lock))
  const record = safePath(program, spec.plugin_record)
  await mkdir(join(record, '..'), { recursive: true })
  await writeFile(record, JSON.stringify({ dsh_version: spec.version, bridge_protocol: spec.bridge_protocol, adapter: spec.adapter }))
  const fakeNpm = join(root, 'npm-cli.mjs')
  await writeFile(fakeNpm, `import {readFileSync,mkdirSync,writeFileSync} from 'node:fs';import {join,dirname} from 'node:path';
  if(process.argv[2]==='--version'){console.log('11.17.0');process.exit(0)}
  const target=process.argv[process.argv.indexOf('--prefix')+1];const pkg=JSON.parse(readFileSync(join(target,'package.json'),'utf8'));
  writeFileSync(join(target,'arguments.json'),JSON.stringify(process.argv.slice(2)));
  const spec=${JSON.stringify(spec)};
  spec.version=pkg.dependencies[spec.package_name];
  for(const path of [spec.cli_entry,spec.schedule,...spec.required_files]){const file=join(target,path);mkdirSync(dirname(file),{recursive:true});writeFileSync(file,'');}
  const packageFile=join(target,spec.modules,spec.package_name,'package.json');writeFileSync(packageFile,JSON.stringify({name:spec.package_name,version:pkg.dependencies[spec.package_name],type:'module'}));
  writeFileSync(join(target,spec.modules,'pnpm/package.json'),JSON.stringify({name:'pnpm',version:pkg.dependencies.pnpm}));
  writeFileSync(join(target,spec.cli_entry),'export async function runCli(options){if(!options.packageManager.args.some(arg=>arg.endsWith("pnpm.mjs")))throw new Error("missing package manager");console.log(${spec.version})}'.replace('console.log(${spec.version})','console.log('+JSON.stringify(spec.version)+')'));`)
  for (const declaration of [lock, { ...lock, version: '1.0.0', package_manager: 'pnpm@12.0.0' }]) {
    const expected = targetSpec(declaration)
    await writeFile(join(program, 'config/dsh/runtime.lock.json'), JSON.stringify(declaration))
    await writeFile(record, JSON.stringify({ dsh_version: expected.version, bridge_protocol: expected.bridge_protocol, adapter: expected.adapter }))
    const candidate = join(target, declaration.version)
    await mkdir(join(candidate, spec.modules), { recursive: true })
    await writeFile(join(candidate, spec.modules, 'interrupted.txt'), '残缺安装不可直接启用')
    await install(program, candidate, fakeNpm, root)
    await assert.rejects(access(join(candidate, spec.modules, 'interrupted.txt')))
    const metadata = JSON.parse(await readFile(join(candidate, 'runtime.json'), 'utf8'))
    assert.ok(isCurrent(metadata, expected))
    const pkg = JSON.parse(await readFile(join(candidate, 'package.json'), 'utf8'))
    assert.equal(pkg.dependencies[expected.package_name], expected.version)
    assert.equal(pkg.dependencies.pnpm, declaration.package_manager.slice(5))
  }
})

test('安装失败保留现用组件与可复用缓存', async () => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-install-failure-'))
  const program = join(root, 'app'), spec = targetSpec(lock)
  await mkdir(join(program, 'config/dsh'), { recursive: true })
  await writeFile(join(program, 'config/dsh/runtime.lock.json'), JSON.stringify(lock))
  const record = safePath(program, spec.plugin_record)
  await mkdir(join(record, '..'), { recursive: true })
  await writeFile(record, JSON.stringify({ dsh_version: spec.version, bridge_protocol: spec.bridge_protocol, adapter: spec.adapter }))
  await mkdir(join(root, 'runtimes/dsh'), { recursive: true })
  await mkdir(join(root, 'cache/npm'), { recursive: true })
  await writeFile(join(root, 'runtimes/dsh/old.txt'), '现用组件')
  await writeFile(join(root, 'cache/npm/reusable'), '可复用缓存')
  const npm = join(root, 'failed-npm.mjs')
  await writeFile(npm, "if(process.argv[2]==='--version'){console.log('11.17.0')}else{process.exit(1)}")
  await assert.rejects(install(program, join(root, 'tmp/candidate'), npm, root), /安装失败/u)
  // retry cache（重试缓存）：第二次安装仍指向同一工作区缓存，不受残缺解压目录影响。
  await writeFile(npm, "import {writeFileSync,existsSync} from 'node:fs';import {join} from 'node:path';if(process.argv[2]==='--version'){console.log('11.17.0')}else{if(!existsSync(join(process.env.NPM_CONFIG_CACHE,'reusable')))throw new Error('cache lost');writeFileSync(join(process.env.NPM_CONFIG_CACHE,'retry-confirmed'),'已复用缓存');process.exit(1)}")
  await assert.rejects(install(program, join(root, 'tmp/candidate'), npm, root), /安装失败/u)
  assert.equal(await readFile(join(root, 'cache/npm/retry-confirmed'), 'utf8'), '已复用缓存')
  assert.equal(await readFile(join(root, 'runtimes/dsh/old.txt'), 'utf8'), '现用组件')
  assert.equal(await readFile(join(root, 'cache/npm/reusable'), 'utf8'), '可复用缓存')
})
