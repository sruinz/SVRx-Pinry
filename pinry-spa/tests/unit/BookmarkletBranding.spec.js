/* eslint-env jest */
import fs from 'fs';
import path from 'path';


describe('SVRx Pinry bookmarklet branding', () => {
  afterEach(() => {
    document.body.innerHTML = '';
    document.onkeyup = null;
  });

  it('renders the Korean-first SVRx Pinry toolbar labels', () => {
    document.body.innerHTML = '<script id="pinry-bookmarklet" src="https://pinry.example/static/js/bookmarklet.js"></script>';
    const script = fs.readFileSync(path.resolve(
      __dirname,
      '../../../pinry/static/js/bookmarklet.js',
    ), 'utf8');

    window.eval(script); // eslint-disable-line no-eval

    const bar = document.getElementById('pinry-bar');
    expect(bar.childNodes[0].textContent).toBe('SVRx Pinry 북마클릿');
    expect(bar.lastChild.textContent).toBe('⇕ 크기');
  });
});
