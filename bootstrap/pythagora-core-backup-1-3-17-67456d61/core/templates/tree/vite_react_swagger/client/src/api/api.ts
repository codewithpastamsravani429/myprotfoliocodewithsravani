import axios, { AxiosRequestConfig, AxiosError } from 'axios';
import JSONbig from 'json-bigint';

{% if options.auth_type == "api_key" %}
const API_KEY = import.meta.env.VITE_API_KEY;
{% endif %}

{% if options.auth_type != "login" %}
const EXTERNAL_API_URL = import.meta.env.VITE_EXTERNAL_API_URL;
{% endif %}

const localApi = axios.create({
  headers: {
    'Content-Type': 'application/json',
  },
  validateStatus: (status) => {
    return status >= 200 && status < 300;
  },
  transformResponse: [(data) => JSONbig.parse(data)]
});

{% if options.auth_type != "login" %}
const externalApi = axios.create({
  baseURL: EXTERNAL_API_URL,
  headers: {
    'Content-Type': 'application/json',
  },
  validateStatus: (status) => {
    return status >= 200 && status < 300;
  },
});
{% endif %}


let accessToken: string | null = null;

const getApiInstance = (url: string) => {
  {% if options.auth_type != "login" %}
return isAuthEndpoint(url) ? localApi : externalApi;
  {% else %}
  return localApi;
  {% endif %}
};

const isAuthEndpoint = (url: string): boolean => {
  return url.includes("/api/auth");
};

{% if options.auth %}
const setupInterceptors = (apiInstance: typeof axios) => {
  apiInstance.interceptors.request.use(
    (config: AxiosRequestConfig): AxiosRequestConfig => {
{% if options.auth_type == "api_key" %}
      if (!isAuthEndpoint(config.url || '')) {
          config.baseURL = EXTERNAL_API_URL;
        if (config.headers && API_KEY) {
          config.headers['api_key'] = API_KEY;
        }
      }
{% endif %}

      if (!accessToken) {
        accessToken = localStorage.getItem('accessToken');
      }
      if (accessToken && config.headers) {
        config.headers.Authorization = `Bearer ${accessToken}`;
      }

      return config;
    },
    (error: AxiosError): Promise<AxiosError> => Promise.reject(error)
  );

    {% if options.auth %}
    apiInstance.interceptors.response.use(
    (response) => response,
    async (error: AxiosError): Promise<any> => {
      const originalRequest = error.config as AxiosRequestConfig & { _retry?: boolean };

      if ([401, 403].includes(error.response?.status) && !originalRequest._retry) {
        originalRequest._retry = true;

        try {
            if (isAuthEndpoint(originalRequest.url || '')) {
                const { data } = await localApi.post(`/api/auth/refresh`, {
                refreshToken: localStorage.getItem('refreshToken'),
                });
                accessToken = data.data.accessToken;
                localStorage.setItem('accessToken', accessToken);
                localStorage.setItem('refreshToken', data.data.refreshToken);
            }

          if (originalRequest.headers) {
            originalRequest.headers.Authorization = `Bearer ${accessToken}`;
            {% if options.auth_type == "api_key" %}
            if (!isAuthEndpoint(originalRequest.url || '') && API_KEY) {
              originalRequest.headers['api_key'] = API_KEY;
            }
            {% endif %}
          }
          return getApiInstance(originalRequest.url || '')(originalRequest);
        } catch (err) {
          localStorage.removeItem('refreshToken');
          localStorage.removeItem('accessToken');
          accessToken = null;
          window.location.href = '/login';
          return Promise.reject(err);
        }
      }

      return Promise.reject(error);
    }
  );
    {% endif %}
};

setupInterceptors(localApi);

{% if options.auth_type != "login" %}
setupInterceptors(externalApi);
{% endif %}

{% endif %}

const api = {
  request: (config: AxiosRequestConfig) => {
    const apiInstance = getApiInstance(config.url || '');
    return apiInstance(config);
  },
  get: (url: string, config?: AxiosRequestConfig) => {
    const apiInstance = getApiInstance(url);
    return apiInstance.get(url, config);
  },
  post: (url: string, data?: any, config?: AxiosRequestConfig) => {
    const apiInstance = getApiInstance(url);
    return apiInstance.post(url, data, config);
  },
  put: (url: string, data?: any, config?: AxiosRequestConfig) => {
    const apiInstance = getApiInstance(url);
    return apiInstance.put(url, data, config);
  },
  delete: (url: string, config?: AxiosRequestConfig) => {
    const apiInstance = getApiInstance(url);
    return apiInstance.delete(url, config);
  },
};

export default api;
