// Offline ESM bindings and eager-initialization analysis. Uses Node's bundled
// Acorn parser: no npm install, network request, or application execution.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const acorn = require('internal/deps/acorn/acorn/dist/acorn');

const isFunction = (node) => node && /^(FunctionDeclaration|FunctionExpression|ArrowFunctionExpression)$/.test(node.type);
function names(pattern) {
  if (!pattern) return [];
  if (pattern.type === 'Identifier') return [pattern.name];
  if (pattern.type === 'RestElement') return names(pattern.argument);
  if (pattern.type === 'AssignmentPattern') return names(pattern.left);
  if (pattern.type === 'ArrayPattern') return pattern.elements.flatMap(names);
  if (pattern.type === 'ObjectPattern') return pattern.properties.flatMap((item) => names(item.type === 'RestElement' ? item.argument : item.value));
  return [];
}
function children(node) {
  return Object.entries(node).filter(([key]) => !['loc', 'start', 'end'].includes(key)).flatMap(([, value]) =>
    Array.isArray(value) ? value.filter((item) => item && typeof item.type === 'string') : value && typeof value.type === 'string' ? [value] : []);
}
function declaration(node) { return node.type === 'ExportNamedDeclaration' || node.type === 'ExportDefaultDeclaration' ? node.declaration : node; }
function analyze(directory) {
  const modules = new Map();
  for (const file of fs.readdirSync(directory).filter((name) => name.endsWith('.js')).sort()) {
    const source = fs.readFileSync(path.join(directory, file), 'utf8');
    const ast = acorn.parse(source, { ecmaVersion: 'latest', sourceType: 'module', locations: true });
    const mod = { file, source, ast, bindings: new Map(), imports: new Map(), exports: new Map(), dependencies: [], references: [] };
    for (const statement of ast.body) {
      if (statement.source) mod.dependencies.push(statement.source.value);
      if (statement.type === 'ImportDeclaration') {
        for (const spec of statement.specifiers) {
          const imported = spec.type === 'ImportDefaultSpecifier' ? 'default' : spec.type === 'ImportNamespaceSpecifier' ? '*' : spec.imported.name ?? spec.imported.value;
          const binding = { kind: 'import', source: statement.source.value, imported, node: spec, module: mod };
          mod.bindings.set(spec.local.name, binding); mod.imports.set(spec.local.name, binding);
        }
      }
      const decl = declaration(statement);
      if (decl?.type === 'VariableDeclaration') for (const item of decl.declarations) {
        for (const name of names(item.id)) mod.bindings.set(name, { kind: decl.kind, node: item, module: mod });
      }
      if (decl?.id && /^(FunctionDeclaration|ClassDeclaration)$/.test(decl.type)) mod.bindings.set(decl.id.name, { kind: decl.type === 'FunctionDeclaration' ? 'function' : 'class', node: decl, module: mod });
      if (statement.type === 'ExportNamedDeclaration') {
        if (decl?.type === 'VariableDeclaration') for (const item of decl.declarations) for (const name of names(item.id)) mod.exports.set(name, { local: name });
        else if (decl?.id) mod.exports.set(decl.id.name, { local: decl.id.name });
        for (const spec of statement.specifiers || []) mod.exports.set(spec.exported.name ?? spec.exported.value, { local: spec.local.name ?? spec.local.value, source: statement.source?.value });
      }
      if (statement.type === 'ExportDefaultDeclaration') mod.exports.set('default', decl?.id ? { local: decl.id.name } : { node: decl, module: mod, kind: isFunction(decl) ? 'function' : 'const' });
    }
    modules.set(file, mod);
  }
  if (!modules.size) throw new Error('No JavaScript modules found; scan did not run');
  function resolveModule(mod, source) {
    if (!source.startsWith('./') && !source.startsWith('../')) throw new Error(`${mod.file}: external import is not supported: ${source}`);
    const resolved = path.resolve(directory, path.dirname(mod.file), source);
    if (path.dirname(resolved) !== path.resolve(directory)) throw new Error(`${mod.file}: import outside module directory: ${source}`);
    const target = modules.get(path.basename(resolved));
    if (!target) throw new Error(`${mod.file}: missing module ${source}`);
    return target;
  }
  function exported(mod, name, visited = new Set()) {
    const key = `${mod.file}:${name}`;
    if (visited.has(key)) return null;
    visited.add(key);
    const item = mod.exports.get(name);
    if (item?.source) return exported(resolveModule(mod, item.source), item.local, visited);
    if (item?.local) {
      const binding = mod.bindings.get(item.local);
      if (binding?.kind === 'import') return exported(resolveModule(mod, binding.source), binding.imported, visited);
      return binding;
    }
    if (item) return item;
    for (const statement of mod.ast.body.filter((item) => item.type === 'ExportAllDeclaration')) {
      const found = exported(resolveModule(mod, statement.source.value), name, visited);
      if (found) return found;
    }
    return null;
  }

  // Lexical scopes, not token occurrence: parameters, destructuring, catch
  // bindings, local declarations and object/member keys cannot require imports.
  function collectReferences(mod) {
    const root = { parent: null, bindings: new Set(mod.bindings.keys()) };
    const lookup = (scope, name) => scope && (scope.bindings.has(name) || lookup(scope.parent, name));
    function prebind(scope, statements) {
      for (const statement of statements) {
        const decl = declaration(statement);
        if (decl?.type === 'VariableDeclaration') for (const item of decl.declarations) for (const name of names(item.id)) scope.bindings.add(name);
        if (decl?.id && /^(FunctionDeclaration|ClassDeclaration)$/.test(decl.type)) scope.bindings.add(decl.id.name);
      }
    }
    function hoistVars(scope, node) {
      if (isFunction(node)) return;
      if (node.type === 'VariableDeclaration' && node.kind === 'var') for (const item of node.declarations) for (const name of names(item.id)) scope.bindings.add(name);
      for (const child of children(node)) hoistVars(scope, child);
    }
    function patternExpressions(pattern, scope) {
      if (!pattern) return;
      if (pattern.type === 'AssignmentPattern') { walk(pattern.right, scope); patternExpressions(pattern.left, scope); }
      else if (pattern.type === 'ObjectPattern') for (const item of pattern.properties) { if (item.computed) walk(item.key, scope); patternExpressions(item.value || item.argument, scope); }
      else if (pattern.type === 'ArrayPattern') for (const item of pattern.elements) patternExpressions(item, scope);
      else if (pattern.type === 'RestElement') patternExpressions(pattern.argument, scope);
    }
    function walk(node, scope) {
      if (!node) return;
      if (node.type === 'Identifier') { if (!lookup(scope, node.name)) mod.references.push({ name: node.name, line: node.loc.start.line }); return; }
      if (node.type === 'ImportDeclaration' || node.type === 'ExportAllDeclaration') return;
      if (node.type === 'ExportNamedDeclaration' || node.type === 'ExportDefaultDeclaration') { if (node.declaration) walk(node.declaration, scope); return; }
      if (isFunction(node)) {
        const inner = { parent: scope, bindings: new Set(node.params.flatMap(names)) };
        if (node.id) inner.bindings.add(node.id.name);
        if (node.type !== 'ArrowFunctionExpression') inner.bindings.add('arguments');
        hoistVars(inner, node.body);
        for (const param of node.params) patternExpressions(param, inner);
        walk(node.body, inner); return;
      }
      if (node.type === 'BlockStatement' || node.type === 'Program') {
        const inner = node.type === 'Program' ? scope : { parent: scope, bindings: new Set() };
        prebind(inner, node.body); for (const item of node.body) walk(item, inner); return;
      }
      if (node.type === 'VariableDeclaration') { for (const item of node.declarations) { patternExpressions(item.id, scope); walk(item.init, scope); } return; }
      if (node.type === 'MemberExpression') { walk(node.object, scope); if (node.computed) walk(node.property, scope); return; }
      if (node.type === 'Property' || node.type === 'MethodDefinition' || node.type === 'PropertyDefinition') { if (node.computed) walk(node.key, scope); walk(node.value, scope); return; }
      if (node.type === 'ClassDeclaration' || node.type === 'ClassExpression') {
        walk(node.superClass, scope);
        const inner = { parent: scope, bindings: new Set(node.id ? [node.id.name] : []) };
        walk(node.body, inner); return;
      }
      if (node.type === 'CatchClause') { const inner = { parent: scope, bindings: new Set(names(node.param)) }; walk(node.body, inner); return; }
      if (['ForStatement', 'ForOfStatement', 'ForInStatement'].includes(node.type)) {
        const inner = { parent: scope, bindings: new Set() };
        prebind(inner, [node.init || node.left].filter(Boolean));
        for (const child of children(node)) walk(child, inner); return;
      }
      if (node.type === 'LabeledStatement') { walk(node.body, scope); return; }
      if (node.type === 'BreakStatement' || node.type === 'ContinueStatement' || node.type === 'MetaProperty') return;
      for (const child of children(node)) walk(child, scope);
    }
    hoistVars(root, mod.ast); walk(mod.ast, root);
  }
  for (const mod of modules.values()) collectReferences(mod);
  const owners = new Map();
  for (const mod of modules.values()) for (const [name, binding] of mod.bindings) if (binding.kind !== 'import') {
    if (!owners.has(name)) owners.set(name, []); owners.get(name).push(mod.file);
  }
  const esm = [];
  for (const mod of modules.values()) {
    for (const [local, binding] of mod.imports) {
      const target = resolveModule(mod, binding.source);
      if (binding.imported !== '*' && !exported(target, binding.imported)) esm.push({ file: mod.file, line: binding.node.loc.start.line, message: `import ${binding.imported} as ${local}: ${target.file} does not export it` });
    }
    for (const ref of mod.references) if (owners.has(ref.name) && !owners.get(ref.name).includes(mod.file)) esm.push({ file: mod.file, line: ref.line, message: `unbound ${ref.name}; module declarations: ${owners.get(ref.name).join(', ')}` });
  }

  // Follow actual static-import DFS from main.js, matching ESM dependency
  // evaluation order. Only eager identifier reads are TDZ candidates; closures
  // are not evaluated until directly invoked. Hoisted function exports are safe.
  const tdz = [], evaluated = new Set(), active = new Set(), initialized = new Set(), calling = new Set();
  function read(mod, node, shadowed) {
    if (shadowed.has(node.name)) return;
    let binding = mod.bindings.get(node.name);
    if (!binding) return;
    if (binding.kind === 'import') {
      if (binding.imported === '*') return;
      binding = exported(resolveModule(mod, binding.source), binding.imported);
    }
    if (!binding || ['function', 'var'].includes(binding.kind)) return;
    // All names in a destructuring declarator share an initialization site.
    if (!initialized.has(`${binding.module.file}:${binding.node.start}`)) tdz.push({ file: mod.file, line: node.loc.start.line, message: `eager read ${node.name} before ${binding.module.file}:${binding.node.loc.start.line} initializes` });
  }
  function callable(mod, node) {
    if (isFunction(node)) return { node, module: mod };
    if (node?.type !== 'Identifier') return null;
    let binding = mod.bindings.get(node.name);
    if (binding?.kind === 'import') binding = exported(resolveModule(mod, binding.source), binding.imported);
    if (!binding) return null;
    const fn = isFunction(binding.node) ? binding.node : binding.node.init;
    return isFunction(fn) ? { node: fn, module: binding.module } : null;
  }
  function eager(mod, node, shadowed = new Set()) {
    if (!node || isFunction(node)) return;
    if (node.type === 'Identifier') { read(mod, node, shadowed); return; }
    if (node.type === 'ImportDeclaration' || node.type === 'ExportAllDeclaration') return;
    if (node.type === 'ExportNamedDeclaration' || node.type === 'ExportDefaultDeclaration') { eager(mod, node.declaration, shadowed); return; }
    if (node.type === 'VariableDeclaration') {
      for (const decl of node.declarations) { eager(mod, decl.init, shadowed); initialized.add(`${mod.file}:${decl.start}`); }
      return;
    }
    if (node.type === 'ClassDeclaration' || node.type === 'ClassExpression') {
      eager(mod, node.superClass, shadowed);
      for (const item of node.body.body) {
        if (item.computed) eager(mod, item.key, shadowed);
        if (item.type === 'StaticBlock') for (const statement of item.body) eager(mod, statement, shadowed);
        else if (item.static) eager(mod, item.value, shadowed);
      }
      initialized.add(`${mod.file}:${node.start}`); return;
    }
    if (node.type === 'MemberExpression') { eager(mod, node.object, shadowed); if (node.computed) eager(mod, node.property, shadowed); return; }
    if (node.type === 'Property' || node.type === 'PropertyDefinition' || node.type === 'MethodDefinition') { if (node.computed) eager(mod, node.key, shadowed); eager(mod, node.value, shadowed); return; }
    if (node.type === 'CallExpression' || node.type === 'NewExpression') {
      eager(mod, node.callee, shadowed); for (const arg of node.arguments) eager(mod, arg, shadowed);
      const fn = node.callee.type === 'Identifier' && shadowed.has(node.callee.name) ? null : callable(mod, node.callee);
      if (fn) {
        const key = `${fn.module.file}:${fn.node.start}`;
        if (!calling.has(key)) {
          calling.add(key);
          const local = new Set(fn.node.params.flatMap(names));
          function localNames(item) {
            if (isFunction(item)) { if (item.id) local.add(item.id.name); return; }
            if (item.type === 'VariableDeclaration') for (const decl of item.declarations) for (const name of names(decl.id)) local.add(name);
            for (const child of children(item)) localNames(child);
          }
          localNames(fn.node.body);
          for (const param of fn.node.params) if (param.type === 'AssignmentPattern') eager(fn.module, param.right, local);
          eager(fn.module, fn.node.body, local); calling.delete(key);
        }
      }
      return;
    }
    for (const child of children(node)) eager(mod, child, shadowed);
  }
  function evaluateModule(mod) {
    if (evaluated.has(mod.file) || active.has(mod.file)) return;
    active.add(mod.file);
    for (const source of mod.dependencies) evaluateModule(resolveModule(mod, source));
    for (const statement of mod.ast.body) eager(mod, statement);
    active.delete(mod.file); evaluated.add(mod.file);
  }
  // Entry-order-sensitive constant reads deserve a separate cycle check even
  // when main.js happens to initialize the producer first.
  function reaches(from, target, visited = new Set()) {
    if (from.file === target.file) return true;
    if (visited.has(from.file)) return false;
    visited.add(from.file);
    return from.dependencies.some((source) => reaches(resolveModule(from, source), target, visited));
  }
  function cyclicInitializer(mod, node) {
    if (!node || isFunction(node)) return;
    if (node.type === 'Identifier') {
      const imported = mod.imports.get(node.name);
      if (!imported || imported.imported === '*') return;
      const target = exported(resolveModule(mod, imported.source), imported.imported);
      if (target && !['function', 'var'].includes(target.kind) && reaches(target.module, mod)) {
        tdz.push({ file: mod.file, line: node.loc.start.line, message: `entry-sensitive cyclic initializer reads ${node.name} from ${target.module.file}:${target.node.loc.start.line}` });
      }
      return;
    }
    if (node.type === 'MemberExpression') { cyclicInitializer(mod, node.object); if (node.computed) cyclicInitializer(mod, node.property); return; }
    if (node.type === 'Property') { if (node.computed) cyclicInitializer(mod, node.key); cyclicInitializer(mod, node.value); return; }
    for (const child of children(node)) cyclicInitializer(mod, child);
  }
  for (const mod of modules.values()) for (const statement of mod.ast.body) {
    const decl = declaration(statement);
    if (decl?.type === 'VariableDeclaration') for (const item of decl.declarations) cyclicInitializer(mod, item.init);
  }
  const entry = modules.get('main.js'); if (entry) evaluateModule(entry);
  for (const mod of modules.values()) evaluateModule(mod);
  return { esm, tdz: [...new Map(tdz.map((item) => [`${item.file}:${item.line}:${item.message}`, item])).values()], files: [...modules.values()].map((mod) => ({ file: mod.file, imports: mod.imports.size, exports: mod.exports.size })) };
}
async function main() {
  const directory = path.resolve(process.argv[2]);
  const result = analyze(directory);
  // The engine is the authority on static export linking, including aliases,
  // namespace imports, default imports, export lists and re-exports. Link only:
  // evaluating application code would touch browser state and is forbidden here.
  const context = vm.createContext({}); const cache = new Map();
  const load = (filename) => {
    if (!cache.has(filename)) cache.set(filename, new vm.SourceTextModule(fs.readFileSync(filename, 'utf8'), { context, identifier: filename }));
    return cache.get(filename);
  };
  const linker = (source, referencing) => load(path.resolve(path.dirname(referencing.identifier), source));
  for (const item of result.files) {
    const mod = load(path.join(directory, item.file));
    if (mod.status === 'unlinked') {
      try { await mod.link(linker); }
      catch (error) { result.esm.push({ file: item.file, line: 1, message: `ESM linker: ${error.message}` }); break; }
    }
  }
  process.stdout.write(JSON.stringify(result));
}
main().catch((error) => { console.error(error.stack || error); process.exitCode = 2; });
