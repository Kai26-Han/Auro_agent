import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

const source = await readFile(new URL('../src/connectorSelection.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
const {connectorSelections, missingConnectorTools, toggleConnectorSelection, MAX_CONNECTOR_TOOLS} = await import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));
const connector = (id, count, status='connected') => ({id,name:id,status,transport:'http',tools:Array.from({length:count},(_,i)=>({id:`${id}-${i}`,name:`tool_${i}`,policy:i===0?'disabled':i===1?'confirm':'read'}))});
const a=connector('github',46), b=connector('notes',4);
let rows=connectorSelections([a,b],[]);
let value=toggleConnectorSelection(rows[0],[]);
assert.equal(value.length,45); // Bulk selection must not truncate to the old 20 limit.
assert.ok(!value.includes('github-0'));
assert.ok(value.includes('github-1')); // Confirm-policy tools retain their policy.
assert.equal(a.tools[0].policy,'disabled');
value=toggleConnectorSelection(connectorSelections([a,b],value)[1],value);
assert.equal(value.length,48);
value=toggleConnectorSelection(connectorSelections([a,b],value)[0],value);
assert.deepEqual(value,['notes-1','notes-2','notes-3']);
const grants=['github-2','github-0'];
assert.deepEqual(toggleConnectorSelection(connectorSelections([a],[],grants)[0],[]),['github-2']);
assert.deepEqual(toggleConnectorSelection(connectorSelections([a],[],[])[0],[]),[]);
assert.deepEqual(toggleConnectorSelection(connectorSelections([{...a,status:'disconnected'}],[])[0],[]),[]);
const legacy=['github-0','github-2','notes-1','missing'];
assert.deepEqual(toggleConnectorSelection(connectorSelections([{...a,status:'disconnected'},b],legacy,[])[0],legacy),['notes-1','missing']);
assert.deepEqual(missingConnectorTools([a,b],legacy),['missing']);
const big=connector('big',MAX_CONNECTOR_TOOLS+1);
assert.equal(toggleConnectorSelection(connectorSelections([big],[])[0],[]).length,MAX_CONNECTOR_TOOLS);
assert.throws(()=>toggleConnectorSelection(connectorSelections([big],['notes-1'])[0],['notes-1']),/connector_tool_limit/);
console.log('Connector selection: bulk, permissions, legacy, disconnect, missing references and limit checks passed.');
