const STORAGE_KEY = 'pinry-theme';

export function loadTheme() {
  let theme = 'dark';
  try {
    if (window.localStorage.getItem(STORAGE_KEY) === 'light') theme = 'light';
  } catch (error) {
    // 저장소가 차단된 브라우저에서는 기존 다크 화면을 유지한다.
  }
  document.documentElement.dataset.theme = theme;
  return theme;
}

export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    window.localStorage.setItem(STORAGE_KEY, theme);
  } catch (error) {
    // 저장할 수 없어도 현재 화면의 테마 전환은 허용한다.
  }
}
