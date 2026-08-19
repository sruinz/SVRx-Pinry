import utils from './browser';

function getDocumentScrollTop() {
  const doc = document.documentElement;
  return (window.pageYOffset || doc.scrollTop) - (doc.clientTop || 0);
}

function onScroll2Bottom(callback) {
  const windowPosition = getDocumentScrollTop() + window.innerHeight;
  const bottom = utils.getDocumentHeight() - 100;
  if (windowPosition > bottom) {
    callback();
  }
}

function bindScroll2Bottom(callback) {
  const handler = () => {
    onScroll2Bottom(callback);
  };
  window.addEventListener('scroll', handler);
  return () => {
    window.removeEventListener('scroll', handler);
  };
}

export default {
  bindScroll2Bottom,
};
