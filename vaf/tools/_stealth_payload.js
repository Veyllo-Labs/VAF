(() => {
const opts = {"navigator_hardware_concurrency": true, "navigator_languages_override": ["en-US", "en"], "navigator_platform": "Win32", "navigator_user_agent": null, "navigator_vendor": null, "webgl_renderer": "Intel Iris OpenGL Engine", "webgl_vendor": "Intel Inc.", "script_logging": false};
/**
 * A set of shared utility functions specifically to modify native browser APIs without leaving traces.
 */
const utils = {};

/**
 * Wraps a JS Proxy Handler and strips it's presence from error stacks, in case the traps throw.
 * The presence of a JS Proxy can be revealed as it shows up in error stack traces.
 *
 * @param {object} handler - The JS Proxy handler to wrap
 */
utils.stripProxyFromErrors = (handler = {}) => {
  const handler_name = (Math.random() + 1).toString(36).substring(2);
  window[handler_name] = {}
  // We wrap each trap in the handler in a try/catch and modify the error stack if they throw
  const traps = Object.getOwnPropertyNames(handler);
  traps.forEach((trap) => {
    window[handler_name][trap] = function () {
      try {
        // Forward the call to the defined proxy handler
        return handler[trap].apply(this, arguments || []);
      } catch (err) {
        // Stack traces differ per browser, we only support chromium based ones currently
        if (!err || !err.stack || !err.stack.includes(`at `)) {
          throw err;
        }

        // When something throws within one of our traps the Proxy will show up in error stacks
        // An earlier implementation of this code would simply strip lines with a blacklist,
        // but it makes sense to be more surgical here and only remove lines related to our Proxy.
        // We try to use a known "anchor" line for that and strip it with everything above it.
        // If the anchor line cannot be found for some reason we fall back to our blacklist approach.

        const stripWithBlacklist = (stack) => {
          const blacklist = [
            `at Reflect.${trap} `, // e.g. Reflect.get or Reflect.apply
            `at Object.${trap} `, // e.g. Object.get or Object.apply
            `at window.<computed>.<computed> [as ${trap}] `, // caused by this very wrapper :-)
          ];
          return (
            err.stack
              .split("\n")
              // Always remove the first (file) line in the stack (guaranteed to be our proxy)
              .filter((line, index) => index !== 1)
              // Check if the line starts with one of our blacklisted strings
              .filter((line) => !blacklist.some((bl) => line.trim().startsWith(bl)))
              .join("\n")
          );
        };

        const stripWithAnchor = (stack) => {
          const stackArr = stack.split("\n");
          const anchor = `at window.<computed>.<computed> [as ${trap}] `; // Known first Proxy line in chromium
          const anchorIndex = stackArr.findIndex((line) => line.trim().startsWith(anchor));
          if (anchorIndex === -1) {
            return false; // 404, anchor not found
          }
          // Strip everything from the top until we reach the anchor line
          // Note: We're keeping the 1st line (zero index) as it's unrelated (e.g. `TypeError`)
          stackArr.splice(1, anchorIndex);
          return stackArr.join("\n");
        };

        // Try using the anchor method, fallback to blacklist if necessary
        err.stack = stripWithAnchor(err.stack) || stripWithBlacklist(err.stack);

        throw err; // Re-throw our now sanitized error
      }
    };
  });
  return window[handler_name];
};

/**
 * Strip error lines from stack traces until (and including) a known line the stack.
 *
 * @param {object} err - The error to sanitize
 * @param {string} anchor - The string the anchor line starts with
 */
utils.stripErrorWithAnchor = (err, anchor) => {
  const stackArr = err.stack.split("\n");
  const anchorIndex = stackArr.findIndex((line) => line.trim().startsWith(anchor));
  if (anchorIndex === -1) {
    return err; // 404, anchor not found
  }
  // Strip everything from the top until we reach the anchor line (remove anchor line as well)
  // Note: We're keeping the 1st line (zero index) as it's unrelated (e.g. `TypeError`)
  stackArr.splice(1, anchorIndex);
  err.stack = stackArr.join("\n");
  return err;
};

/**
 * Replace the property of an object in a stealthy way.
 *
 * Note: You also want to work on the prototype of an object most often,
 * as you'd otherwise leave traces (e.g. showing up in Object.getOwnPropertyNames(obj)).
 *
 * @see https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Object/defineProperty
 *
 * @example
 * replaceProperty(WebGLRenderingContext.prototype, 'getParameter', { value: "alice" })
 * // or
 * replaceProperty(Object.getPrototypeOf(navigator), 'languages', { get: () => ['en-US', 'en'] })
 *
 * @param {object} obj - The object which has the property to replace
 * @param {string} propName - The property name to replace
 * @param {object} descriptorOverrides - e.g. { value: "alice" }
 */
utils.replaceProperty = (obj, propName, descriptorOverrides = {}) => {
  return Object.defineProperty(obj, propName, {
    // Copy over the existing descriptors (writable, enumerable, configurable, etc)
    ...(Object.getOwnPropertyDescriptor(obj, propName) || {}),
    // Add our overrides (e.g. value, get())
    ...descriptorOverrides,
  });
};

/**
 * Preload a cache of function copies and data.
 *
 * For a determined enough observer it would be possible to overwrite and sniff usage of functions
 * we use in our internal Proxies, to combat that we use a cached copy of those functions.
 *
 * This is evaluated once per execution context (e.g. window)
 */
utils.preloadCache = () => {
  if (utils.cache) {
    return;
  }
  utils.cache = {
    // Used in our proxies
    Reflect: {
      get: Reflect.get.bind(Reflect),
      apply: Reflect.apply.bind(Reflect),
    },
    // Used in `makeNativeString`
    nativeToStringStr: Function.toString.toString(), // => `function toString() { [native code] }`
  };
};

/**
 * Utility function to generate a cross-browser `toString` result representing native code.
 *
 * There's small differences: Chromium uses a single line, whereas FF & Webkit uses multiline strings.
 * To future-proof this we use an existing native toString result as the basis.
 *
 * The only advantage we have over the other team is that our JS runs first, hence we cache the result
 * of the native toString result once, so they cannot spoof it afterwards and reveal that we're using it.
 *
 * Note: Whenever we add a `Function.prototype.toString` proxy we should preload the cache before,
 * by executing `utils.preloadCache()` before the proxy is applied (so we don't cause recursive lookups).
 *
 * @example
 * makeNativeString('foobar') // => `function foobar() { [native code] }`
 *
 * @param {string} [name] - Optional function name
 */
utils.makeNativeString = (name = "") => {
  // Cache (per-window) the original native toString or use that if available
  utils.preloadCache();
  return utils.cache.nativeToStringStr.replace("toString", name || "");
};

/**
 * Helper function to modify the `toString()` result of the provided object.
 *
 * Note: Use `utils.redirectToString` instead when possible.
 *
 * There's a quirk in JS Proxies that will cause the `toString()` result to differ from the vanilla Object.
 * If no string is provided we will generate a `[native code]` thing based on the name of the property object.
 *
 * @example
 * patchToString(WebGLRenderingContext.prototype.getParameter, 'function getParameter() { [native code] }')
 *
 * @param {object} obj - The object for which to modify the `toString()` representation
 * @param {string} str - Optional string used as a return value
 */
utils.patchToString = (obj, str = "") => {
  utils.preloadCache();

  const toStringProxy = new Proxy(Function.prototype.toString, {
    apply: function (target, ctx) {
      // This fixes e.g. `HTMLMediaElement.prototype.canPlayType.toString + ""`
      if (ctx === Function.prototype.toString) {
        return utils.makeNativeString("toString");
      }
      // `toString` targeted at our proxied Object detected
      if (ctx === obj) {
        // We either return the optional string verbatim or derive the most desired result automatically
        return str || utils.makeNativeString(obj.name);
      }
      // Check if the toString protype of the context is the same as the global prototype,
      // if not indicates that we are doing a check across different windows., e.g. the iframeWithdirect` test case
      const hasSameProto = Object.getPrototypeOf(Function.prototype.toString).isPrototypeOf(ctx.toString); // eslint-disable-line no-prototype-builtins
      if (!hasSameProto) {
        // Pass the call on to the local Function.prototype.toString instead
        return ctx.toString();
      }
      return target.call(ctx);
    },
  });
  utils.replaceProperty(Function.prototype, "toString", {
    value: toStringProxy,
  });
};

/**
 * Make all nested functions of an object native.
 *
 * @param {object} obj
 */
utils.patchToStringNested = (obj = {}) => {
  return utils.execRecursively(obj, ["function"], utils.patchToString);
};

/**
 * Redirect toString requests from one object to another.
 *
 * @param {object} proxyObj - The object that toString will be called on
 * @param {object} originalObj - The object which toString result we wan to return
 */
utils.redirectToString = (proxyObj, originalObj) => {
  utils.preloadCache();

  const toStringProxy = new Proxy(Function.prototype.toString, {
    apply: function (target, ctx) {
      // This fixes e.g. `HTMLMediaElement.prototype.canPlayType.toString + ""`
      if (ctx === Function.prototype.toString) {
        return utils.makeNativeString("toString");
      }

      // `toString` targeted at our proxied Object detected
      if (ctx === proxyObj) {
        const fallback = () =>
          originalObj && originalObj.name
            ? utils.makeNativeString(originalObj.name)
            : utils.makeNativeString(proxyObj.name);

        // Return the toString representation of our original object if possible
        return originalObj + "" || fallback();
      }

      // Check if the toString protype of the context is the same as the global prototype,
      // if not indicates that we are doing a check across different windows., e.g. the iframeWithdirect` test case
      const hasSameProto = Object.getPrototypeOf(Function.prototype.toString).isPrototypeOf(ctx.toString); // eslint-disable-line no-prototype-builtins
      if (!hasSameProto) {
        // Pass the call on to the local Function.prototype.toString instead
        return ctx.toString();
      }

      return target.call(ctx);
    },
  });
  utils.replaceProperty(Function.prototype, "toString", {
    value: toStringProxy,
  });
};

/**
 * All-in-one method to replace a property with a JS Proxy using the provided Proxy handler with traps.
 *
 * Will stealthify these aspects (strip error stack traces, redirect toString, etc).
 * Note: This is meant to modify native Browser APIs and works best with prototype objects.
 *
 * @example
 * replaceWithProxy(WebGLRenderingContext.prototype, 'getParameter', proxyHandler)
 *
 * @param {object} obj - The object which has the property to replace
 * @param {string} propName - The name of the property to replace
 * @param {object} handler - The JS Proxy handler to use
 */
utils.replaceWithProxy = (obj, propName, handler) => {
  utils.preloadCache();
  const originalObj = obj[propName];
  const proxyObj = new Proxy(obj[propName], utils.stripProxyFromErrors(handler));

  utils.replaceProperty(obj, propName, { value: proxyObj });
  utils.redirectToString(proxyObj, originalObj);

  return true;
};

/**
 * All-in-one method to mock a non-existing property with a JS Proxy using the provided Proxy handler with traps.
 *
 * Will stealthify these aspects (strip error stack traces, redirect toString, etc).
 *
 * @example
 * mockWithProxy(chrome.runtime, 'sendMessage', function sendMessage() {}, proxyHandler)
 *
 * @param {object} obj - The object which has the property to replace
 * @param {string} propName - The name of the property to replace or create
 * @param {object} pseudoTarget - The JS Proxy target to use as a basis
 * @param {object} handler - The JS Proxy handler to use
 */
utils.mockWithProxy = (obj, propName, pseudoTarget, handler) => {
  utils.preloadCache();
  const proxyObj = new Proxy(pseudoTarget, utils.stripProxyFromErrors(handler));

  utils.replaceProperty(obj, propName, { value: proxyObj });
  utils.patchToString(proxyObj);

  return true;
};

/**
 * All-in-one method to create a new JS Proxy with stealth tweaks.
 *
 * This is meant to be used whenever we need a JS Proxy but don't want to replace or mock an existing known property.
 *
 * Will stealthify certain aspects of the Proxy (strip error stack traces, redirect toString, etc).
 *
 * @example
 * createProxy(navigator.mimeTypes.__proto__.namedItem, proxyHandler) // => Proxy
 *
 * @param {object} pseudoTarget - The JS Proxy target to use as a basis
 * @param {object} handler - The JS Proxy handler to use
 */
utils.createProxy = (pseudoTarget, handler) => {
  utils.preloadCache();
  const proxyObj = new Proxy(pseudoTarget, utils.stripProxyFromErrors(handler));
  utils.patchToString(proxyObj);
  return proxyObj;
};

/**
 * Helper function to split a full path to an Object into the first part and property.
 *
 * @example
 * splitObjPath(`HTMLMediaElement.prototype.canPlayType`)
 * // => {objName: "HTMLMediaElement.prototype", propName: "canPlayType"}
 *
 * @param {string} objPath - The full path to an object as dot notation string
 */
utils.splitObjPath = (objPath) => ({
  // Remove last dot entry (property) ==> `HTMLMediaElement.prototype`
  objName: objPath.split(".").slice(0, -1).join("."),
  // Extract last dot entry ==> `canPlayType`
  propName: objPath.split(".").slice(-1)[0],
});

/**
 * Convenience method to replace a property with a JS Proxy using the provided objPath.
 *
 * Supports a full path (dot notation) to the object as string here, in case that makes it easier.
 *
 * @example
 * replaceObjPathWithProxy('WebGLRenderingContext.prototype.getParameter', proxyHandler)
 *
 * @param {string} objPath - The full path to an object (dot notation string) to replace
 * @param {object} handler - The JS Proxy handler to use
 */
utils.replaceObjPathWithProxy = (objPath, handler) => {
  const { objName, propName } = utils.splitObjPath(objPath);
  const obj = eval(objName); // eslint-disable-line no-eval
  return utils.replaceWithProxy(obj, propName, handler);
};

/**
 * Traverse nested properties of an object recursively and apply the given function on a whitelist of value types.
 *
 * @param {object} obj
 * @param {array} typeFilter - e.g. `['function']`
 * @param {Function} fn - e.g. `utils.patchToString`
 */
utils.execRecursively = (obj = {}, typeFilter = [], fn) => {
  function recurse(obj) {
    for (const key in obj) {
      if (obj[key] === undefined) {
        continue;
      }
      if (obj[key] && typeof obj[key] === "object") {
        recurse(obj[key]);
      } else {
        if (obj[key] && typeFilter.includes(typeof obj[key])) {
          fn.call(this, obj[key]);
        }
      }
    }
  }

  recurse(obj);
  return obj;
};

/**
 * Everything we run through e.g. `page.evaluate` runs in the browser context, not the NodeJS one.
 * That means we cannot just use reference variables and functions from outside code, we need to pass everything as a parameter.
 *
 * Unfortunately, the data we can pass is only allowed to be of primitive types, regular functions don't survive the built-in serialization process.
 * This utility function will take an object with functions and stringify them, so we can pass them down unharmed as strings.
 *
 * We use this to pass down our utility functions as well as any other functions (to be able to split up code better).
 *
 * @see utils.materializeFns
 *
 * @param {object} fnObj - An object containing functions as properties
 */
utils.stringifyFns = (fnObj = { hello: () => "world" }) => {
  // Object.fromEntries() polyfill (in 6 lines) - supported only in Node v12+, modern browsers are fine
  // https://github.com/feross/fromentries
  return (Object.fromEntries || fromEntries)(
    Object.entries(fnObj)
      .filter(([key, value]) => typeof value === "function")
      .map(([key, value]) => [key, value.toString()]) // eslint-disable-line no-eval
  );
};

/**
 * Utility function to reverse the process of `utils.stringifyFns`.
 * Will materialize an object with stringified functions (supports classic and fat arrow functions).
 *
 * @param {object} fnStrObj - An object containing stringified functions as properties
 */
utils.materializeFns = (fnStrObj = { hello: "() => 'world'" }) => {
  return Object.fromEntries(
    Object.entries(fnStrObj).map(([key, value]) => {
      if (value.startsWith("function")) {
        // some trickery is necessary to make oldschool functions work :-)
        return [key, eval(`() => ${value}`)()]; // eslint-disable-line no-eval
      } else {
        // arrow functions just work
        return [key, eval(value)]; // eslint-disable-line no-eval
      }
    })
  );
};

utils.arrayEqual = (arr1, arr2) => arr1.length === arr2.length && arr1.every((value, index) => value === arr2[index]);

const log = (...args) => opts.script_logging && console.log("[playwright-stealth]:", ...args);
const warn = (...args) => opts.script_logging && console.warn("[playwright-stealth]:", ...args);

log(JSON.stringify(opts));

generateFunctionMocks = (proto, itemMainProp, dataArray) => ({
  item: utils.createProxy(proto.item, {
    apply(target, ctx, args) {
      if (!args.length) {
        throw new TypeError(
          `Failed to execute 'item' on '${proto[Symbol.toStringTag]}': 1 argument required, but only 0 present.`
        );
      }
      // Special behavior alert:
      // - Vanilla tries to cast strings to Numbers (only integers!) and use them as property index lookup
      // - If anything else than an integer (including as string) is provided it will return the first entry
      const isInteger = args[0] && Number.isInteger(Number(args[0])); // Cast potential string to number first, then check for integer
      // Note: Vanilla never returns `undefined`
      return (isInteger ? dataArray[Number(args[0])] : dataArray[0]) || null;
    }
  }),
  /** Returns the MimeType object with the specified name. */
  namedItem: utils.createProxy(proto.namedItem, {
    apply(target, ctx, args) {
      if (!args.length) {
        throw new TypeError(
          `Failed to execute 'namedItem' on '${proto[Symbol.toStringTag]}': 1 argument required, but only 0 present.`
        );
      }
      return dataArray.find((mt) => mt[itemMainProp] === args[0]) || null; // Not `undefined`!
    }
  }),
  /** Does nothing and shall return nothing */
  refresh: proto.refresh
    ? utils.createProxy(proto.refresh, {
      apply(target, ctx, args) {
        return undefined;
      }
    })
    : undefined
});

function generateMagicArray(
  dataArray = [],
  proto = MimeTypeArray.prototype,
  itemProto = MimeType.prototype,
  itemMainProp = "type"
) {
  // Quick helper to set props with the same descriptors vanilla is using
  const defineProp = (obj, prop, value) =>
    Object.defineProperty(obj, prop, {
      value,
      writable: false,
      enumerable: false, // Important for mimeTypes & plugins: `JSON.stringify(navigator.mimeTypes)`
      configurable: false
    });

  // Loop over our fake data and construct items
  const makeItem = (data) => {
    const item = {};
    for (const prop of Object.keys(data)) {
      if (prop.startsWith("__")) {
        continue;
      }
      defineProp(item, prop, data[prop]);
    }
    // navigator.plugins[i].length should always be 1
    if (itemProto === Plugin.prototype) {
      defineProp(item, "length", 1);
    }
    // We need to spoof a specific `MimeType` or `Plugin` object
    return Object.create(itemProto, Object.getOwnPropertyDescriptors(item));
  };

  const magicArray = [];

  // Loop through our fake data and use that to create convincing entities
  dataArray.forEach((data) => {
    magicArray.push(makeItem(data));
  });

  // Add direct property access  based on types (e.g. `obj['application/pdf']`) afterwards
  magicArray.forEach((entry) => {
    defineProp(magicArray, entry[itemMainProp], entry);
  });

  // This is the best way to fake the type to make sure this is false: `Array.isArray(navigator.mimeTypes)`
  const magicArrayObj = Object.create(proto, {
    ...Object.getOwnPropertyDescriptors(magicArray),

    // There's one ugly quirk we unfortunately need to take care of:
    // The `MimeTypeArray` prototype has an enumerable `length` property,
    // but headful Chrome will still skip it when running `Object.getOwnPropertyNames(navigator.mimeTypes)`.
    // To strip it we need to make it first `configurable` and can then overlay a Proxy with an `ownKeys` trap.
    length: {
      value: magicArray.length,
      writable: false,
      enumerable: false,
      configurable: true // Important to be able to use the ownKeys trap in a Proxy to strip `length`
    }
  });

  // Generate our functional function mocks :-)
  const functionMocks = generateFunctionMocks(proto, itemMainProp, magicArray);

  // Override custom object with proxy
  return new Proxy(magicArrayObj, {
    get(target, key = "") {
      // Redirect function calls to our custom proxied versions mocking the vanilla behavior
      if (key === "item") {
        return functionMocks.item;
      }
      if (key === "namedItem") {
        return functionMocks.namedItem;
      }
      if (proto === PluginArray.prototype && key === "refresh") {
        return functionMocks.refresh;
      }
      // Everything else can pass through as normal
      return utils.cache.Reflect.get(...arguments);
    },
    ownKeys(target) {
      // There are a couple of quirks where the original property demonstrates "magical" behavior that makes no sense
      // This can be witnessed when calling `Object.getOwnPropertyNames(navigator.mimeTypes)` and the absense of `length`
      // My guess is that it has to do with the recent change of not allowing data enumeration and this being implemented weirdly
      // For that reason we just completely fake the available property names based on our data to match what regular Chrome is doing
      // Specific issues when not patching this: `length` property is available, direct `types` props (e.g. `obj['application/pdf']`) are missing
      const keys = [];
      const typeProps = magicArray.map((mt) => mt[itemMainProp]);
      typeProps.forEach((_, i) => keys.push(`${i}`));
      typeProps.forEach((propName) => keys.push(propName));
      return keys;
    }
  });
}

log("loading chrome.app.js");

if (!window.chrome) {
  // Use the exact property descriptor found in headful Chrome
  // fetch it via `Object.getOwnPropertyDescriptor(window, 'chrome')`
  Object.defineProperty(window, "chrome", {
    writable: true,
    enumerable: true,
    configurable: false, // note!
    value: {} // We'll extend that later
  });
}

// app in window.chrome means we're running headful and don't need to mock anything
if (!("app" in window.chrome)) {
  const makeError = {
    ErrorInInvocation: (fn) => {
      const err = new TypeError(`Error in invocation of app.${fn}()`);
      return utils.stripErrorWithAnchor(err, `at ${fn} (eval at <anonymous>`);
    }
  };

  const APP_STATIC_DATA = JSON.parse(
    `
{
  "isInstalled": false,
  "InstallState": {
    "DISABLED": "disabled",
    "INSTALLED": "installed",
    "NOT_INSTALLED": "not_installed"
  },
  "RunningState": {
    "CANNOT_RUN": "cannot_run",
    "READY_TO_RUN": "ready_to_run",
    "RUNNING": "running"
  }
}
        `.trim()
  );

  window.chrome.app = {
    ...APP_STATIC_DATA,

    get isInstalled() {
      return false;
    },

    getDetails: function getDetails() {
      if (arguments.length) {
        throw makeError.ErrorInInvocation(`getDetails`);
      }
      return null;
    },
    getIsInstalled: function getDetails() {
      if (arguments.length) {
        throw makeError.ErrorInInvocation(`getIsInstalled`);
      }
      return false;
    },
    runningState: function getDetails() {
      if (arguments.length) {
        throw makeError.ErrorInInvocation(`runningState`);
      }
      return "cannot_run";
    }
  };
  utils.patchToStringNested(window.chrome.app);
}

log("loading chrome.csi.js");

if (!window.chrome) {
  // Use the exact property descriptor found in headful Chrome
  // fetch it via `Object.getOwnPropertyDescriptor(window, 'chrome')`
  Object.defineProperty(window, "chrome", {
    writable: true,
    enumerable: true,
    configurable: false, // note!
    value: {} // We'll extend that later
  });
}

// Check if we're running headful and don't need to mock anything
// Check that the Navigation Timing API v1 is available, we need that
if (!("csi" in window.chrome) && window.performance?.timing) {
  const { csi_timing } = window.performance;

  log("loading chrome.csi.js");
  window.chrome.csi = function() {
    return {
      onloadT: csi_timing?.domContentLoadedEventEnd,
      startE: csi_timing?.navigationStart,
      pageT: Date.now() - csi_timing?.navigationStart,
      tran: 15 // transition? seems constant
    };
  };
  utils.patchToString(window.chrome.csi);
}

log("loading chrome.hairline.js");
// inspired by: https://intoli.com/blog/making-chrome-headless-undetectable/
const elementDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype,
  "offsetHeight");

utils.replaceProperty(HTMLDivElement.prototype, "offsetHeight", {
  get: function() {
    // hmmm not sure about this
    if (this.id === "modernizr") {
      return 1;
    }
    return elementDescriptor.get.apply(this);
  }
});

log("loading chrome.load.times.js");

if (!window.chrome) {
  // Use the exact property descriptor found in headful Chrome
  // fetch it via `Object.getOwnPropertyDescriptor(window, 'chrome')`
  Object.defineProperty(window, "chrome", {
    writable: true,
    enumerable: true,
    configurable: false, // note!
    value: {} // We'll extend that later
  });
}

// That means we're running headful and don't need to mock anything
if ("loadTimes" in window.chrome) {
  warn("skipping chrome loadtimes update, running in headful mode");
}

// Check that the Navigation Timing API v1 + v2 is available, we need that
if (window.performance?.timing || window.PerformancePaintTiming) {
  const { performance } = window;

  // Some stuff is not available on about:blank as it requires a navigation to occur,
  // let's harden the code to not fail then:
  const ntEntryFallback = {
    nextHopProtocol: "h2",
    type: "other"
  };

  // The API exposes some funky info regarding the connection
  const protocolInfo = {
    get connectionInfo() {
      const ntEntry = performance.getEntriesByType("navigation")[0] ||
        ntEntryFallback;
      return ntEntry.nextHopProtocol;
    },
    get npnNegotiatedProtocol() {
      // NPN is deprecated in favor of ALPN, but this implementation returns the
      // HTTP/2 or HTTP2+QUIC/39 requests negotiated via ALPN.
      const ntEntry = performance.getEntriesByType("navigation")[0] ||
        ntEntryFallback;
      return ["h2", "hq"].includes(ntEntry.nextHopProtocol) ?
        ntEntry.nextHopProtocol :
        "unknown";
    },
    get navigationType() {
      const ntEntry = performance.getEntriesByType("navigation")[0] ||
        ntEntryFallback;
      return ntEntry.type;
    },
    get wasAlternateProtocolAvailable() {
      // The Alternate-Protocol header is deprecated in favor of Alt-Svc
      // (https://www.mnot.net/blog/2016/03/09/alt-svc), so technically this
      // should always return false.
      return false;
    },
    get wasFetchedViaSpdy() {
      // SPDY is deprecated in favor of HTTP/2, but this implementation returns
      // true for HTTP/2 or HTTP2+QUIC/39 as well.
      const ntEntry = performance.getEntriesByType("navigation")[0] ||
        ntEntryFallback;
      return ["h2", "hq"].includes(ntEntry.nextHopProtocol);
    },
    get wasNpnNegotiated() {
      // NPN is deprecated in favor of ALPN, but this implementation returns true
      // for HTTP/2 or HTTP2+QUIC/39 requests negotiated via ALPN.
      const ntEntry = performance.getEntriesByType("navigation")[0] ||
        ntEntryFallback;
      return ["h2", "hq"].includes(ntEntry.nextHopProtocol);
    }
  };

  const { timing } = window.performance;

  // Truncate number to specific number of decimals, most of the `loadTimes` stuff has 3
  function toFixed(num, fixed) {
    const re = new RegExp("^-?\\d+(?:.\\d{0," + (fixed || -1) + "})?");
    return num.toString().match(re)[0];
  }

  const timingInfo = {
    get firstPaintAfterLoadTime() {
      // This was never actually implemented and always returns 0.
      return 0;
    },
    get requestTime() {
      return timing.navigationStart / 1000;
    },
    get startLoadTime() {
      return timing.navigationStart / 1000;
    },
    get commitLoadTime() {
      return timing.responseStart / 1000;
    },
    get finishDocumentLoadTime() {
      return timing.domContentLoadedEventEnd / 1000;
    },
    get finishLoadTime() {
      return timing.loadEventEnd / 1000;
    },
    get firstPaintTime() {
      const fpEntry = performance.getEntriesByType("paint")[0] || {
        startTime: timing.loadEventEnd / 1000 // Fallback if no navigation occured (`about:blank`)
      };
      return toFixed((fpEntry.startTime + performance.timeOrigin) / 1000, 3);
    }
  };

  window.chrome.loadTimes = function() {
    return {
      ...protocolInfo,
      ...timingInfo
    };
  };
  utils.patchToString(window.chrome.loadTimes);
}

log("loading iframe.contentWindow.js");

try {
  // Adds a contentWindow proxy to the provided iframe element
  const addContentWindowProxy = (iframe) => {
    const contentWindowProxy = {
      get(target, key) {
        // Now to the interesting part:
        // We actually make this thing behave like a regular iframe window,
        // by intercepting calls to e.g. `.self` and redirect it to the correct thing. :)
        // That makes it possible for these assertions to be correct:
        // iframe.contentWindow.self === window.top // must be false
        if (key === "self") {
          return this;
        }
        // iframe.contentWindow.frameElement === iframe // must be true
        if (key === "frameElement") {
          return iframe;
        }
        return Reflect.get(target, key);
      }
    };

    if (!iframe.contentWindow) {
      const proxy = new Proxy(window, contentWindowProxy);
      Object.defineProperty(iframe, "contentWindow", {
        get() {
          return proxy;
        },
        set(newValue) {
          return newValue; // contentWindow is immutable
        },
        enumerable: true,
        configurable: false
      });
    }
  };

  // Handles iframe element creation, augments `srcdoc` property so we can intercept further
  const handleIframeCreation = (target, thisArg, args) => {
    const iframe = target.apply(thisArg, args);

    // We need to keep the originals around
    const _iframe = iframe;
    const _srcdoc = _iframe.srcdoc;

    // Add hook for the srcdoc property
    // We need to be very surgical here to not break other iframes by accident
    Object.defineProperty(iframe, "srcdoc", {
      configurable: true, // Important, so we can reset this later
      get: function() {
        return _iframe.srcdoc;
      },
      set: function(newValue) {
        addContentWindowProxy(this);
        // Reset property, the hook is only needed once
        Object.defineProperty(iframe, "srcdoc", {
          configurable: false,
          writable: false,
          value: _srcdoc
        });
        _iframe.srcdoc = newValue;
      }
    });
    return iframe;
  };

  // Adds a hook to intercept iframe creation events
  const addIframeCreationSniffer = () => {
    /* global document */
    const createElementHandler = {
      // Make toString() native
      get(target, key) {
        return Reflect.get(target, key);
      },
      apply: function(target, thisArg, args) {
        const isIframe = args && args.length && `${args[0]}`.toLowerCase() ===
          "iframe";
        if (!isIframe) {
          // Everything as usual
          return target.apply(thisArg, args);
        } else {
          return handleIframeCreation(target, thisArg, args);
        }
      }
    };
    // All this just due to iframes with srcdoc bug
    utils.replaceWithProxy(document, "createElement", createElementHandler);
  };

  // Let's go
  addIframeCreationSniffer();
} catch (err) {
  // console.warn(err)
}

log("loading media.codec.js");
/**
 * Input might look funky, we need to normalize it so e.g. whitespace isn't an issue for our spoofing.
 *
 * @example
 * video/webm; codecs="vp8, vorbis"
 * video/mp4; codecs="avc1.42E01E"
 * audio/x-m4a;
 * audio/ogg; codecs="vorbis"
 * @param {String} arg
 */
const parseInput = (arg) => {
  const [mime, codecStr] = arg.trim().split(";");
  let codecs = [];
  if (codecStr && codecStr.includes("codecs=\"")) {
    codecs = codecStr.trim().
      replace(`codecs="`, "").
      replace(`"`, "").
      trim().
      split(",").
      filter((x) => !!x).
      map((x) => x.trim());
  }
  return {
    mime,
    codecStr,
    codecs
  };
};

const canPlayType = {
  // Intercept certain requests
  apply: function(target, ctx, args) {
    if (!args || !args.length) {
      return target.apply(ctx, args);
    }
    const { mime, codecs } = parseInput(args[0]);
    // This specific mp4 codec is missing in Chromium
    if (mime === "video/mp4") {
      if (codecs.includes("avc1.42E01E")) {
        return "probably";
      }
    }
    // This mimetype is only supported if no codecs are specified
    if (mime === "audio/x-m4a" && !codecs.length) {
      return "maybe";
    }

    // This mimetype is only supported if no codecs are specified
    if (mime === "audio/aac" && !codecs.length) {
      return "probably";
    }
    // Everything else as usual
    return target.apply(ctx, args);
  }
};

/* global HTMLMediaElement */
utils.replaceWithProxy(HTMLMediaElement.prototype, "canPlayType", canPlayType);

log("loading navigator.languages.js");
log(navigator.languages, opts.navigator_languages_override);
if (utils.arrayEqual(navigator.languages, opts.navigator_languages_override)) {
  log("not patching navigator.languages, assuming CLI args were used instead");
} else {
  utils.replaceProperty(Object.getPrototypeOf(navigator), "languages", {
    get: () => opts.navigator_languages_override
  });
}

log("loading navigator.permissions.js");

const handler = {
  apply: function(target, ctx, args) {
    const param = (args || [])[0];

    if (param && param.name && param.name === "notifications") {
      const result = { state: Notification.permission };
      Object.setPrototypeOf(result, PermissionStatus.prototype);
      return Promise.resolve(result);
    }

    return utils.cache.Reflect.apply(...arguments);
  }
};

utils.replaceWithProxy(
  window.navigator.permissions.__proto__, // eslint-disable-line no-proto
  "query",
  handler
);

log(`loading navigator.platform.js with opt: ${opts.navigator_platform}`);

if (opts.navigator_platform && navigator.platform !== opts.navigator_platform) {
  utils.replaceProperty(Object.getPrototypeOf(navigator), "platform", {
    get: () => opts.navigator_platform
  });
}

log("loading navigator.plugins.js");

const data = {
  mimeTypes: [
    {
      type: "application/pdf",
      suffixes: "pdf",
      description: "",
      __pluginName: "Chrome PDF Viewer"
    },
    {
      type: "application/x-google-chrome-pdf",
      suffixes: "pdf",
      description: "Portable Document Format",
      __pluginName: "Chrome PDF Plugin"
    },
    {
      type: "application/x-nacl",
      suffixes: "",
      description: "Native Client Executable",
      __pluginName: "Native Client"
    },
    {
      type: "application/x-pnacl",
      suffixes: "",
      description: "Portable Native Client Executable",
      __pluginName: "Native Client"
    }
  ],
  plugins: [
    {
      name: "Chrome PDF Plugin",
      filename: "internal-pdf-viewer",
      description: "Portable Document Format",
      __mimeTypes: ["application/x-google-chrome-pdf"]
    },
    {
      name: "Chrome PDF Viewer",
      filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai",
      description: "",
      __mimeTypes: ["application/pdf"]
    },
    {
      name: "Native Client",
      filename: "internal-nacl-plugin",
      description: "",
      __mimeTypes: ["application/x-nacl", "application/x-pnacl"]
    }
  ]
};

// That means we're running headful
const hasPlugins = "plugins" in navigator && navigator.plugins.length;
if (!hasPlugins) {
  const mimeTypes = generateMagicArray(data.mimeTypes, MimeTypeArray.prototype,
    MimeType.prototype, "type");
  const plugins = generateMagicArray(data.plugins, PluginArray.prototype,
    Plugin.prototype, "name");

  // Plugin and MimeType cross-reference each other, let's do that now
  // Note: We're looping through `data.plugins` here, not the generated `plugins`
  for (const pluginData of data.plugins) {
    pluginData.__mimeTypes.forEach((type, index) => {
      plugins[pluginData.name][index] = mimeTypes[type];
      plugins[type] = mimeTypes[type];
      Object.defineProperty(mimeTypes[type], "enabledPlugin", {
        value: JSON.parse(JSON.stringify(plugins[pluginData.name])),
        writable: false,
        enumerable: false, // Important: `JSON.stringify(navigator.plugins)`
        configurable: false
      });
    });
  }

  const patchNavigator = (name, value) =>
    utils.replaceProperty(Object.getPrototypeOf(navigator), name, {
      get() {
        return value;
      }
    });

  patchNavigator("mimeTypes", mimeTypes);
  patchNavigator("plugins", plugins);
}

log("loading navigator.userAgent.js");
// replace Headless references in default useragent
const current_ua = navigator.userAgent;
utils.replaceProperty(Object.getPrototypeOf(navigator), "userAgent", {
  get: () => opts.navigator_user_agent ||
    current_ua.replace("HeadlessChrome/", "Chrome/")
});

log("loading navigator.userAgentData.js");

const originalUserAgentData = navigator.userAgentData;

if (originalUserAgentData) {
  /**
   * Helper to replace HeadlessChrome with Google Chrome in brand lists
   * @param {Array<{brand: string, version: string}>} list - Array of UADataBrand objects
   */
  const filterBrandList = (list) =>
    list.map((b) => (b.brand === "HeadlessChrome" ? { brand: "Google Chrome", version: b.version } : b));

  // Get the NavigatorUAData prototype
  const NavigatorUADataProto = Object.getPrototypeOf(originalUserAgentData);

  // Cache original methods before patching
  const originalGetHighEntropyValues = NavigatorUADataProto.getHighEntropyValues;
  const originalToJSON = NavigatorUADataProto.toJSON;
  const originalBrandsDescriptor = Object.getOwnPropertyDescriptor(NavigatorUADataProto, "brands");

  let cachedFilteredBrands = null;
  // Helper to get filtered brands (cached for identity checks)
  const getFilteredBrands = () => {
    if (cachedFilteredBrands === null) {
      const originalBrands = originalBrandsDescriptor.get.call(originalUserAgentData);
      cachedFilteredBrands = filterBrandList(originalBrands);
    }
    return cachedFilteredBrands;
  };

  // Patch getHighEntropyValues to filter HeadlessChrome from results
  utils.replaceProperty(NavigatorUADataProto, "getHighEntropyValues", {
    value: function (hints) {
      return originalGetHighEntropyValues.call(this, hints).then((data) => {
        const newData = { ...data };
        if (newData.brands) {
          newData.brands = filterBrandList(newData.brands);
        }
        if (newData.fullVersionList) {
          newData.fullVersionList = filterBrandList(newData.fullVersionList);
        }
        return newData;
      });
    },
  });

  // Patch toJSON to filter HeadlessChrome
  utils.replaceProperty(NavigatorUADataProto, "toJSON", {
    value: function () {
      const data = originalToJSON.call(this);
      return {
        brands: filterBrandList(data.brands),
        mobile: data.mobile,
        platform: data.platform,
      };
    },
  });

  // Patch brands getter to return filtered array (same instance each call, like real Chrome)
  utils.replaceProperty(NavigatorUADataProto, "brands", {
    get: function () {
      // Return cached filtered brands, computing and freezing on first access
      return getFilteredBrands();
    },
    enumerable: originalBrandsDescriptor.enumerable,
    configurable: originalBrandsDescriptor.configurable,
  });

  utils.replaceProperty(NavigatorUADataProto, "userAgentData", {
    get: () => originalUserAgentData,
  });
}

log("loading navigator.vendor.js");

utils.replaceProperty(Object.getPrototypeOf(navigator), "vendor", {
  get: () => opts.navigator_vendor || "Google Inc."
});

log("loading navigator.webdriver.js");
// this is close to the most accurate way to emulate this: https://stackoverflow.com/a/69533548
// no point "giving ourselves away" if we don't need to mock this values
// techniques exist to detect Object.defineProperty etc., so if we can avoid it we do
// if args include --disable-blink-features=AutomationControlled, we do not need to mock this
if (navigator.webdriver) {
  utils.replaceProperty(Object.getPrototypeOf(navigator), "webdriver", {
    get: new Proxy(
      Object.getOwnPropertyDescriptor(Object.getPrototypeOf(navigator),
        "webdriver").get, {
        apply: (target, thisArg, args) => {
          // emulate getter call validation
          Reflect.apply(target, thisArg, args);
          return false;
        }
      })
  });
} else {
  log("not patching navigator.webdriver, assuming CLI args were used instead");
}

log("loading error.prototype.js");

Object.defineProperty(Error.prototype, "name", {configurable: false, enumerable: false})

log("loading webgl.vendor.js");

const getParameterProxyHandler = {
  apply: function (target, ctx, args) {
    const param = (args || [])[0];
    // UNMASKED_VENDOR_WEBGL
    if (param === 37445) {
      return opts.webgl_vendor || "Intel Inc."; // default in headless: Google Inc.
    }
    // UNMASKED_RENDERER_WEBGL
    if (param === 37446) {
      return opts.webgl_renderer || "Intel Iris OpenGL Engine"; // default in headless: Google SwiftShader
    }
    return utils.cache.Reflect.apply(target, ctx, args);
  },
};

// There's more than one WebGL rendering context
// https://developer.mozilla.org/en-US/docs/Web/API/WebGL2RenderingContext#Browser_compatibility
// To find out the original values here: Object.getOwnPropertyDescriptors(WebGLRenderingContext.prototype.getParameter)
const addProxy = (obj, propName) => {
  utils.replaceWithProxy(obj, propName, getParameterProxyHandler);
};
// For whatever weird reason loops don't play nice with Object.defineProperty, here's the next best thing:
addProxy(WebGLRenderingContext.prototype, "getParameter");
addProxy(WebGL2RenderingContext.prototype, "getParameter");

})();