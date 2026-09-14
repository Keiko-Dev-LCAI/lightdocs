/**
 * Lightdocs document editor — lazy-loaded Tiptap canvas.
 * Images are positioned/resized here; export rebuilds the doc from editor blocks.
 */
const ESM = (pkg) => `https://esm.sh/${pkg}`;

export function isPhone() {
  return window.matchMedia && window.matchMedia("(max-width: 719px)").matches;
}

function markdownToHtml(md, markdownIt) {
  try {
    return markdownIt.render(md || "");
  } catch (_) {
    return `<p>${String(md || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\n/g, "<br>")}</p>`;
  }
}

async function loadTiptap() {
  const [
    core,
    { default: StarterKit },
    { default: TextStyle },
    { default: Color },
    { FontFamily },
    { Underline },
    { default: TextAlign },
  ] = await Promise.all([
    import(ESM("@tiptap/core@2.11.5")),
    import(ESM("@tiptap/starter-kit@2.11.5")),
    import(ESM("@tiptap/extension-text-style@2.11.5")),
    import(ESM("@tiptap/extension-color@2.11.5")),
    import(ESM("@tiptap/extension-font-family@2.11.5")),
    import(ESM("@tiptap/extension-underline@2.11.5")),
    import(ESM("@tiptap/extension-text-align@2.11.5")),
  ]);
  return {
    Editor: core.Editor,
    Node: core.Node,
    Extension: core.Extension,
    mergeAttributes: core.mergeAttributes,
    StarterKit,
    TextStyle,
    Color,
    FontFamily,
    Underline,
    TextAlign,
  };
}

function createFontSize({ Extension }) {
  return Extension.create({
    name: "fontSize",
    addGlobalAttributes() {
      return [
        {
          types: ["textStyle"],
          attributes: {
            fontSize: {
              default: null,
              parseHTML: (el) => el.style.fontSize?.replace(/['"]+/g, "") || null,
              renderHTML: (attrs) =>
                attrs.fontSize ? { style: `font-size: ${attrs.fontSize}` } : {},
            },
          },
        },
      ];
    },
    addCommands() {
      return {
        setFontSize:
          (fontSize) =>
          ({ chain }) =>
            chain().setMark("textStyle", { fontSize }).run(),
        unsetFontSize:
          () =>
          ({ chain }) =>
            chain().setMark("textStyle", { fontSize: null }).removeEmptyTextStyle().run(),
      };
    },
  });
}

function createDocImage({ Node, mergeAttributes }) {
  return Node.create({
    name: "docImage",
    group: "block",
    atom: true,
    draggable: true,
    addAttributes() {
      return {
        src: { default: null },
        alt: { default: "image" },
        width: { default: 320 },
        wrap: { default: "full" },
      };
    },
    parseHTML() {
      return [{ tag: 'div[data-type="doc-image"]' }];
    },
    renderHTML({ HTMLAttributes }) {
      return ["div", mergeAttributes(HTMLAttributes, { "data-type": "doc-image" })];
    },
    addNodeView() {
      return ({ node, getPos, editor }) => {
        const dom = document.createElement("div");
        dom.className = "doc-image-node wrap-" + (node.attrs.wrap || "full");
        dom.dataset.type = "doc-image";
        dom.contentEditable = "false";

        const img = document.createElement("img");
        img.src = node.attrs.src || "";
        img.alt = node.attrs.alt || "image";
        img.draggable = false;
        img.style.width = (node.attrs.width || 320) + "px";
        img.style.maxWidth = "100%";
        img.style.height = "auto";
        img.style.display = "block";

        const handles = document.createElement("div");
        handles.className = "doc-image-handles";
        ["nw", "ne", "sw", "se"].forEach((corner) => {
          const h = document.createElement("span");
          h.className = "doc-image-handle " + corner;
          h.dataset.corner = corner;
          handles.appendChild(h);
        });

        dom.appendChild(img);
        dom.appendChild(handles);

        const select = () => {
          if (typeof getPos === "function") {
            editor.commands.setNodeSelection(getPos());
          }
        };
        dom.addEventListener("click", (e) => {
          e.preventDefault();
          select();
        });

        let resizing = false;
        let startX = 0;
        let startW = 0;
        handles.addEventListener("pointerdown", (e) => {
          if (isPhone()) return;
          e.preventDefault();
          e.stopPropagation();
          select();
          resizing = true;
          startX = e.clientX;
          startW = node.attrs.width || 320;
          dom.setPointerCapture(e.pointerId);
        });
        dom.addEventListener("pointermove", (e) => {
          if (!resizing) return;
          const corner = e.target?.dataset?.corner || "se";
          const dx = e.clientX - startX;
          let next = startW + (corner.includes("w") ? -dx : dx);
          next = Math.max(80, Math.min(next, 720));
          img.style.width = next + "px";
          if (typeof getPos === "function") {
            editor.view.dispatch(
              editor.state.tr.setNodeMarkup(getPos(), undefined, {
                ...node.attrs,
                width: Math.round(next),
              })
            );
          }
        });
        dom.addEventListener("pointerup", () => {
          resizing = false;
        });

        return {
          dom,
          update: (updated) => {
            if (updated.type.name !== "docImage") return false;
            img.src = updated.attrs.src || "";
            img.alt = updated.attrs.alt || "image";
            img.style.width = (updated.attrs.width || 320) + "px";
            dom.className = "doc-image-node wrap-" + (updated.attrs.wrap || "full");
            return true;
          },
          selectNode: () => dom.classList.add("is-selected"),
          deselectNode: () => dom.classList.remove("is-selected"),
          destroy: () => {},
        };
      };
    },
  });
}

function collectMarks(node) {
  const marks = {};
  (node.marks || []).forEach((m) => {
    if (m.type.name === "bold") marks.bold = true;
    if (m.type.name === "italic") marks.italic = true;
    if (m.type.name === "underline") marks.underline = true;
    if (m.type.name === "textStyle") {
      if (m.attrs.color) marks.color = m.attrs.color;
      if (m.attrs.fontFamily) marks.font = m.attrs.fontFamily;
      if (m.attrs.fontSize) {
        const n = parseInt(String(m.attrs.fontSize), 10);
        if (n) marks.fontSize = n;
      }
    }
  });
  return marks;
}

function blockFromTextNode(parentType, textNode, level) {
  // unused helper placeholder
  return null;
}

/**
 * @returns {Promise<{editor, getBlocks, getText, insertImage, setImageAttrs, moveImage, destroy, chain}>}
 */
export async function createLightdocsEditor(opts) {
  const {
    Editor,
    Node,
    Extension,
    mergeAttributes,
    StarterKit,
    TextStyle,
    Color,
    FontFamily,
    Underline,
    TextAlign,
  } = await loadTiptap();
  const DocImage = createDocImage({ Node, mergeAttributes });
  const FontSize = createFontSize({ Extension });
  const html = markdownToHtml(opts.markdown || "", opts.markdownIt);

  const editor = new Editor({
    element: opts.element,
    extensions: [
      StarterKit.configure({ heading: { levels: [1, 2, 3] } }),
      TextStyle,
      Color,
      FontFamily,
      FontSize,
      Underline,
      TextAlign.configure({ types: ["heading", "paragraph"] }),
      DocImage,
    ],
    content: html || "<p></p>",
    editorProps: {
      attributes: { class: "ld-editor-prose" },
    },
    onSelectionUpdate: ({ editor: ed }) => {
      const sel = ed.state.selection;
      if (sel.node && sel.node.type.name === "docImage") {
        opts.onSelectImage &&
          opts.onSelectImage({
            width: sel.node.attrs.width,
            wrap: sel.node.attrs.wrap,
            alt: sel.node.attrs.alt,
            pos: sel.from,
          });
      } else {
        opts.onSelectImage && opts.onSelectImage(null);
      }
    },
  });

  function runsFromInline(node) {
    const runs = [];
    node.forEach((child) => {
      if (child.isText) {
        runs.push({ text: child.text || "", ...collectMarks(child) });
      } else if (child.isTextblock || child.childCount) {
        // nested
        child.forEach((c2) => {
          if (c2.isText) runs.push({ text: c2.text || "", ...collectMarks(c2) });
        });
      }
    });
    if (!runs.length && node.textContent) {
      runs.push({ text: node.textContent });
    }
    return runs;
  }

  function getBlocks() {
    const blocks = [];
    editor.state.doc.forEach((node) => {
      if (node.type.name === "heading") {
        blocks.push({
          type: "heading",
          level: node.attrs.level || 1,
          text: node.textContent,
          runs: runsFromInline(node),
          align: node.attrs.textAlign || null,
        });
      } else if (node.type.name === "bulletList") {
        node.forEach((li) => {
          blocks.push({ type: "bullet", text: li.textContent, runs: runsFromInline(li) });
        });
      } else if (node.type.name === "orderedList") {
        node.forEach((li) => {
          blocks.push({ type: "number", text: li.textContent, runs: runsFromInline(li) });
        });
      } else if (node.type.name === "docImage") {
        const wpx = node.attrs.width || 320;
        blocks.push({
          type: "image",
          src: node.attrs.src,
          alt: node.attrs.alt || "image",
          width_in: Math.max(0.8, Math.min(wpx / 96, 6.5)),
          wrap: node.attrs.wrap || "full",
        });
      } else if (node.type.name === "paragraph") {
        blocks.push({
          type: "paragraph",
          text: node.textContent,
          runs: runsFromInline(node),
          align: node.attrs.textAlign || null,
        });
      } else if (node.isTextblock) {
        blocks.push({
          type: "paragraph",
          text: node.textContent,
          runs: runsFromInline(node),
          align: node.attrs.textAlign || null,
        });
      }
    });
    return blocks.filter(
      (b) => b.type === "image" || (b.text && b.text.trim()) || (b.runs && b.runs.length)
    );
  }

  function insertImage(attrs) {
    editor
      .chain()
      .focus()
      .insertContent({
        type: "docImage",
        attrs: {
          src: attrs.src,
          alt: attrs.alt || "image",
          width: attrs.width || 320,
          wrap: attrs.wrap || "full",
        },
      })
      .run();
  }

  function setImageAttrs(attrs) {
    const sel = editor.state.selection;
    if (!(sel.node && sel.node.type.name === "docImage")) return false;
    editor.chain().focus().updateAttributes("docImage", attrs).run();
    return true;
  }

  function moveImage(dir) {
    const { state } = editor;
    const sel = state.selection;
    if (!(sel.node && sel.node.type.name === "docImage")) return;
    const node = sel.node;
    if (dir < 0) {
      editor
        .chain()
        .focus()
        .command(({ tr, dispatch }) => {
          const p = sel.from;
          if (p <= 1) return false;
          const $pos = tr.doc.resolve(p);
          const before = $pos.nodeBefore;
          if (!before) return false;
          const from = p - before.nodeSize;
          tr.delete(from, p + node.nodeSize);
          tr.insert(from, node.copy(node.content));
          tr.insert(from + node.nodeSize, before);
          if (dispatch) dispatch(tr);
          return true;
        })
        .run();
    } else {
      editor
        .chain()
        .focus()
        .command(({ tr, dispatch }) => {
          const p = sel.from;
          const afterPos = p + node.nodeSize;
          const $pos = tr.doc.resolve(afterPos);
          const after = $pos.nodeAfter;
          if (!after) return false;
          tr.delete(p, afterPos + after.nodeSize);
          tr.insert(p, after);
          tr.insert(p + after.nodeSize, node.copy(node.content));
          if (dispatch) dispatch(tr);
          return true;
        })
        .run();
    }
  }

  function deleteSelectedImage() {
    const sel = editor.state.selection;
    if (!(sel.node && sel.node.type.name === "docImage")) return false;
    editor.chain().focus().deleteSelection().run();
    return true;
  }

  return {
    editor,
    getBlocks,
    getText: () => editor.getText(),
    insertImage,
    setImageAttrs,
    moveImage,
    deleteSelectedImage,
    chain: () => editor.chain().focus(),
    destroy: () => editor.destroy(),
    isPhone,
  };
}
