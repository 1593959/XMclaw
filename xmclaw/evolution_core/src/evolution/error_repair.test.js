'use strict';

const { parseErrors, repairCode } = require('./error_repair');

function assert(condition, message) {
  if (!condition) throw new Error(`ASSERT FAILED: ${message}`);
}

// Test 1: parse SyntaxError
const syntaxErr = parseErrors("SyntaxError: Invalid or unexpected token (E:\\test.js:5:10)");
assert(syntaxErr.length === 1, 'should parse 1 syntax error');
assert(syntaxErr[0].type === 'syntax', 'should be syntax type');

// Test 2: parse ReferenceError
const refErr = parseErrors("ReferenceError: fs is not defined");
assert(refErr.length === 1, 'should parse 1 reference error');
assert(refErr[0].type === 'reference', 'should be reference type');
assert(refErr[0].message.includes('fs'), 'should mention fs');

// Test 3: parse Module not found
const modErr = parseErrors("Error: Cannot find module './helper'");
assert(modErr.length === 1, 'should parse 1 module error');
assert(modErr[0].type === 'module_not_found', 'should be module_not_found type');

// Test 4: repair missing fs require
const codeWithoutFs = "'use strict';\n\nfunction read() { return fs.readFileSync('x'); }";
const repairedFs = repairCode('test.js', codeWithoutFs, [{ type: 'reference', message: 'fs is not defined' }]);
assert(repairedFs.success, 'should repair missing fs');
assert(repairedFs.content.includes("require('node:fs')"), 'should add fs require');

// Test 5: repair illegal \u but preserve legal \u0041
const badUnicode = 'const x = "\\u"; const y = "\\u0041";';
const repairedUnicode = repairCode('test.js', badUnicode, [{ type: 'syntax', message: 'Invalid Unicode escape sequence' }]);
assert(repairedUnicode.success, 'should repair illegal unicode escape');
// content should contain "\\u" (two backslashes + u inside quotes)
assert(repairedUnicode.content.includes('"\\\\u"'), 'illegal \\u should become \\\\u');
// content should contain "\u0041" (one backslash + u0041 inside quotes)
assert(repairedUnicode.content.includes('"\\u0041"'), 'legal \\u0041 should be preserved');

// Test 6: conservative fallback removes AUTO-EVO comments
const withComment = "console.log(1);\n/* [AUTO-EVO] OPTIMIZE\n * something\n */";
const repairedComment = repairCode('test.js', withComment, [{ type: 'syntax', message: 'Unexpected token' }]);
assert(repairedComment.success, 'should apply conservative fallback');
assert(!repairedComment.content.includes('[AUTO-EVO]'), 'should remove auto comment');

console.log('All tests passed!');
