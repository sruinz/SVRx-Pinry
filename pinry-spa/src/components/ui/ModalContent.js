import { h, provide } from 'vue';

export default {
  name: 'ModalContent',
  props: { entry: { type: Object, required: true } },
  setup(props) {
    // 호출자의 provide와 앱 플러그인 문맥을 같은 컴포넌트 트리 안에서 유지한다.
    const provided = props.entry.provides;
    if (provided) {
      const keys = new Set();
      let source = provided;
      while (source) {
        Reflect.ownKeys(source).forEach(key => keys.add(key));
        source = Object.getPrototypeOf(source);
      }
      keys.forEach(key => provide(key, provided[key]));
    }
    provide('isModalActive', () => !props.entry.closed);
    return () => {
      const listeners = {};
      Object.entries(props.entry.events).forEach(([event, handler]) => {
        listeners[`on${event.charAt(0).toUpperCase()}${event.slice(1)}`] = (...args) => {
          if (!props.entry.closed) handler(...args);
        };
      });
      const onClose = (...args) => {
        if (props.entry.closed) return;
        if (props.entry.events.close) props.entry.events.close(...args);
        props.entry.close();
      };
      return h(props.entry.component, { ...props.entry.props, ...listeners, onClose });
    };
  },
};
