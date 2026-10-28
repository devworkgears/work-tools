// エクスポート: 表示中SVGのシリアライズとSVG/PNGダウンロード(FR-5)

function serializeSvg(svgEl) {
  const clone = svgEl.cloneNode(true);
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  clone.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');
  return new XMLSerializer().serializeToString(clone);
}

function downloadBlob(blob, filename) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}

export function downloadSVG(svgEl, filename = 'curve-chart.svg') {
  const str = serializeSvg(svgEl);
  downloadBlob(new Blob([str], { type: 'image/svg+xml;charset=utf-8' }), filename);
}

export function downloadPNG(svgEl, filename = 'curve-chart.png', scale = 3) {
  const str = serializeSvg(svgEl);
  const url = URL.createObjectURL(new Blob([str], { type: 'image/svg+xml;charset=utf-8' }));
  const vb = svgEl.viewBox.baseVal;
  const w = vb?.width || svgEl.clientWidth;
  const h = vb?.height || svgEl.clientHeight;
  const img = new Image();
  img.onload = () => {
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.scale(scale, scale);
    ctx.drawImage(img, 0, 0, w, h);
    URL.revokeObjectURL(url);
    canvas.toBlob((blob) => downloadBlob(blob, filename), 'image/png');
  };
  img.src = url;
}
