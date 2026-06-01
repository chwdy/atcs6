// Shared placeholder types for the editor handler cluster (item #7, Wave 2).
//
// Each editor-*.ts handler takes a back-reference to the Editor instance that
// owns it (`new BlockEditorHandler(this)` in editor.ts). The Editor's own
// renderer/api are typed `any`, and threading a precise `Editor` type through
// all 11 handlers is a separate cleanup, so handlers type the back-reference
// with this permissive placeholder for now.
export type EditorHost = any;
