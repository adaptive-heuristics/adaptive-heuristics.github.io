// In-site PDF viewer (PDF.js, vendored under /assets/pdfjs). Opens /pdf/#page=N at page N.
const root = document.querySelector("[data-pdf-url]");
const msg = document.querySelector("[data-pv-msg]");

async function main() {
  const pdfjsLib = await import("/assets/pdfjs/pdf.min.mjs");
  globalThis.pdfjsLib = pdfjsLib; // pdf_viewer.mjs reads the library from this global
  const { EventBus, PDFLinkService, PDFViewer } = await import("/assets/pdfjs/pdf_viewer.mjs");
  pdfjsLib.GlobalWorkerOptions.workerSrc = "/assets/pdfjs/pdf.worker.min.mjs";

  const container = document.getElementById("pv-container");
  const eventBus = new EventBus();
  const linkService = new PDFLinkService({ eventBus });
  const viewer = new PDFViewer({ container, eventBus, linkService, removePageBorders: true });
  linkService.setViewer(viewer);

  const pageInput = document.querySelector("[data-pv-page]");
  const count = document.querySelector("[data-pv-count]");
  let fitWidth = true;
  const pageFromHash = () => {
    const m = window.location.hash.match(/page=(\d+)/);
    return m ? parseInt(m[1], 10) : 1;
  };

  eventBus.on("pagesinit", () => {
    viewer.currentScaleValue = "page-width";
    const p = Math.min(pageFromHash(), viewer.pagesCount);
    msg.hidden = true;
    // jump after the fit-width rescale has been applied, or the rescale moves the view back
    if (p > 1) requestAnimationFrame(() => setTimeout(() => {
      viewer.currentPageNumber = p;
      const el = container.querySelector('.page[data-page-number="' + p + '"]');
      if (el) container.scrollTop = el.offsetTop - 12;
    }, 0));
  });
  eventBus.on("pagechanging", (e) => { pageInput.value = String(e.pageNumber); });

  const task = pdfjsLib.getDocument({ url: root.dataset.pdfUrl, isEvalSupported: false, enableXfa: false });
  const doc = await task.promise;
  viewer.setDocument(doc);
  linkService.setDocument(doc, null);
  count.textContent = "of " + doc.numPages;

  document.querySelector("[data-pv-prev]").addEventListener("click", () => viewer.previousPage());
  document.querySelector("[data-pv-next]").addEventListener("click", () => viewer.nextPage());
  document.querySelector("[data-pv-in]").addEventListener("click", () => { fitWidth = false; viewer.increaseScale(); });
  document.querySelector("[data-pv-out]").addEventListener("click", () => { fitWidth = false; viewer.decreaseScale(); });
  document.querySelector("[data-pv-fit]").addEventListener("click", () => { fitWidth = true; viewer.currentScaleValue = "page-width"; });
  pageInput.addEventListener("change", () => {
    const n = parseInt(pageInput.value, 10);
    if (n >= 1 && n <= doc.numPages) viewer.currentPageNumber = n;
    else pageInput.value = String(viewer.currentPageNumber);
  });
  window.addEventListener("hashchange", () => { viewer.currentPageNumber = Math.min(pageFromHash(), doc.numPages); });
  let t = null;
  window.addEventListener("resize", () => {
    clearTimeout(t);
    t = setTimeout(() => { if (fitWidth) viewer.currentScaleValue = "page-width"; }, 120);
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.target && /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName)) return;
    if (ev.key === "ArrowRight") { ev.preventDefault(); viewer.nextPage(); }
    if (ev.key === "ArrowLeft") { ev.preventDefault(); viewer.previousPage(); }
  });
}

main().catch((err) => {
  msg.hidden = false;
  msg.innerHTML = "The PDF could not be shown here. <a href=\"" + root.dataset.pdfUrl + "\">Download it instead</a>.";
  console.error(err);
});
