import fs from 'node:fs';
import ts from 'typescript';
const dictionary = JSON.parse(fs.readFileSync('src/locales/en.json','utf8'));
let count=0;
const failures=[];
for(const name of fs.readdirSync('src').filter(n=>/\.(tsx|ts)$/.test(n)&&n!=='i18n.ts')){
 const file=ts.createSourceFile(name,fs.readFileSync('src/'+name,'utf8'),ts.ScriptTarget.Latest,true);
 const visit=node=>{
  if(ts.isCallExpression(node)&&node.expression.getText(file)==='t'&&node.arguments[0]&&ts.isStringLiteral(node.arguments[0])){
   const key=node.arguments[0].text;
   if(/[\u4e00-\u9fff]/.test(key)){
    count++;
    if(!(key in dictionary))failures.push(name+': missing '+key);
    else if(JSON.stringify([...key.matchAll(/\{\d+\}/g)].map(m=>m[0]).sort())!==JSON.stringify([...dictionary[key].matchAll(/\{\d+\}/g)].map(m=>m[0]).sort()))failures.push(name+': placeholder mismatch '+key);
   }
  }
  ts.forEachChild(node,visit);
 };
 visit(file);
}
if(failures.length) { console.error(failures.join('\n')); process.exit(1); }
console.log(`Verified ${count} translated UI strings and interpolation placeholders.`);
