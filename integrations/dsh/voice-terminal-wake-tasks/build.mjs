import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const repo = resolve(here, '../../..')
const typescriptPath = resolve(repo, '.runtime/dsh/source/node_modules/typescript/lib/typescript.js')
const ts = await import(pathToFileURL(typescriptPath).href)
const outputRoot = resolve(process.argv[2] ?? resolve(repo, '.runtime/dsh/bundles/voice-terminal-wake-tasks'))

for (const sourceName of ['task-service.ts', 'index.ts', 'plugin.ts']) {
  const sourcePath = resolve(here, 'src', sourceName)
  const outputPath = resolve(outputRoot, 'lib', sourceName.replace(/\.ts$/u, '.js'))
  const source = await readFile(sourcePath, 'utf8')
  const result = ts.transpileModule(source, {
    fileName: sourcePath,
    reportDiagnostics: true,
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ESNext,
      moduleResolution: ts.ModuleResolutionKind.Bundler,
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
      sourceMap: true,
    },
  })
  const errors = (result.diagnostics ?? []).filter(item => item.category === ts.DiagnosticCategory.Error)
  if (errors.length) throw new Error(errors.map(item => ts.flattenDiagnosticMessageText(item.messageText, '\n')).join('\n'))
  await mkdir(dirname(outputPath), { recursive: true })
  await writeFile(outputPath, `${result.outputText}\n//# sourceURL=${sourcePath.replaceAll('\\', '/') }\n`, 'utf8')
  await writeFile(`${outputPath}.map`, result.sourceMapText ?? '{}', 'utf8')
}

for (const name of ['package.json', 'cordis.patch.yml', 'README.md']) {
  await copyFile(resolve(here, name), resolve(outputRoot, name))
}
