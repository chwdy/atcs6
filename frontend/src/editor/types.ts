// Shared placeholder types for the editor handler cluster (item #7, Wave 2).
//
// Each editor-*.ts handler takes a back-reference to the Editor instance that
// owns it (`new BlockEditorHandler(this)` in the still-classic editor.js). Until
// editor.js itself migrates to TypeScript (Wave 3), the Editor has no real type,
// so handlers type the back-reference with this permissive placeholder. When
// editor.js becomes a module, replace `any` here with the real `Editor` type and
// fix the fallout in one place.
export type EditorHost = any;
