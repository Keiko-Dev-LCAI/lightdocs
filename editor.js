/**
 * Lightdocs document editor — lazy-loaded Tiptap canvas.
 * Charts/images are positioned here; chart pixels come from the server PNG pipeline.
 */
const ESM = (pkg) => `https://esm.sh/${pkg}`;

function isPhone() {
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
    { Editor, Node, mergeAttributes },
    { default: StarterKit },
  ] = await Promise.all([
    import(ESM("@tiptap/core@2.11.5")),
    import(ESM("@tiptap/starter-kit@2.11.5")),
  ]);
  return { Editor, Node, mergeAttributes, StarterKit };
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
        wrap: { default: "full" }, // inline | left | right | full
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

        // Desktop corner resize
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
          const corner = e.target.dataset?.corner || "se";
          const dx = e.clientX - startX;
          const free = e.shiftKey;
          let next = startW + (corner.includes("w") ? -dx : dx);
          next = Math.max(80, Math.min(next, 720));
          if (!free) {
            // aspect locked by CSS height:auto
          }
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

/**
 * @param {object} opts
 * @param {HTMLElement} opts.element
 * @param {string} opts.markdown
 * @param {object} [opts.markdownIt]
 * @param {(sel: object|null) => void} [opts.onSelectImage]
 * @returns {Promise<{editor, getBlocks, getText, insertImage, setImageAttrs, destroy}>}
 */
export async function createLightdocsEditor(opts) {
  const { Editor, Node, mergeAttributes, StarterKit } = await loadTiptap();
  const DocImage = createDocImage({ Node, mergeAttributes });
  const html = markdownToHtml(opts.markdown || "", opts.markdownIt);

  const editor = new Editor({
    element: opts.element,
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3] },
      }),
      DocImage,
    ],
    content: html || "<p></p>",
    editorProps: {
      attributes: {
        class: "ld-editor-prose",
      },
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

  function getBlocks() {
    const blocks = [];
    editor.state.doc.forEach((node) => {
      if (node.type.name === "heading") {
        blocks.push({
          type: "heading",
          level: node.attrs.level || 1,
          text: node.textContent,
        });
      } else if (node.type.name === "bulletList") {
        node.forEach((li) => {
          blocks.push({ type: "bullet", text: li.textContent });
        });
      } else if (node.type.name === "orderedList") {
        node.forEach((li) => {
          blocks.push({ type: "number", text: li.textContent });
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
        blocks.push({ type: "paragraph", text: node.textContent });
      } else if (node.isTextblock) {
        blocks.push({ type: "paragraph", text: node.textContent });
      }
    });
    return blocks.filter((b) => b.type === "image" || (b.text && b.text.trim()) || b.type === "paragraph");
  }

  function insertImage({ src, alt, width, wrap }) {
    editor
      .chain()
      .focus()
      .insertContent({
        type: "docImage",
        attrs: {
          src,
          alt: alt || "image",
          width: width || 320,
          wrap: wrap || "full",
        },
      })
      .run();
  }

  function setImageAttrs(attrs) {
    const sel = editor.state.selection;
    if (!(sel.node && sel.node.type.name === "docImage")) return false;
    editor
      .chain()
      .focus()
      .updateAttributes("docImage", attrs)
      .run();
    return true;
  }

  function moveImage(dir) {
    const { state } = editor;
    const sel = state.selection;
    if (!(sel.node && sel.node.type.name === "docImage")) return;
    const pos = sel.from;
    const node = sel.node;
    let tr = state.tr.delete(pos, pos + node.nodeSize);
    const insertAt =
      dir < 0
        ? Math.max(1, pos - 1)
        : Math.min(tr.doc.content.size, pos + 1);
    // simpler: swap with adjacent block via commands
    if (dir < 0) {
      editor.commands.liftEmptyBlock?.();
      editor.chain().focus().command(({ tr: t, dispatch }) => {
        const p = sel.from;
        if (p <= 1) return false;
        const $pos = t.doc.resolve(p);
        const before = $pos.nodeBefore;
        if (!before) return false;
        const from = p - before.nodeSize;
        const slice = t.doc.slice(from, p + node.nodeSize);
        // fallback: delete and insert before
        t.delete(from, p + node.nodeSize);
        t.insert(from, node);
        if (dispatch) dispatch(t);
        return true;
      }).run();
    } else {
      editor.chain().focus().command(({ tr: t, dispatch }) => {
        const p = sel.from;
        const afterPos = p + node.nodeSize;
        const $pos = t.doc.resolve(afterPos);
        const after = $pos.nodeAfter;
        if (!after) return false;
        t.delete(p, afterPos + after.nodeSize);
        t.insert(p, after);
        t.insert(p + after.nodeSize, node);
        if (dispatch) dispatch(t);
        return true;
      }).run();
    }
  }

  return {
    editor,
    getBlocks,
    getText: () => editor.getText(),
    insertImage,
    setImageAttrs,
    moveImage,
    destroy: () => editor.destroy(),
    isPhone,
  };
}
