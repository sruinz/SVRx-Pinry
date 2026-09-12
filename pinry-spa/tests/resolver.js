module.exports = (request, options) => {
  // Jest 27은 최신 PrimeVue 패키지의 exports 하위 경로를 해석하지 못한다.
  if (/^(primevue|@primevue|@primeuix)\//.test(request)) {
    return require.resolve(request, { paths: [options.basedir] });
  }
  return options.defaultResolver(request, options);
};
