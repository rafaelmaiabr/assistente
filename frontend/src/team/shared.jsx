import React, {useCallback, useEffect, useState} from 'react';
import {api} from './api.js';
import {describeError} from './model.js';

/** Loads a list for the selected company; every failure becomes a sentence, never a stack. */
export function useList(path, deps = []) {
  const [state, setState] = useState({data: [], error: '', loading: true});
  const load = useCallback(async () => {
    setState(s => ({...s, loading: true}));
    try {setState({data: await api.get(path), error: '', loading: false});}
    catch (error) {setState({data: [], error: describeError(error), loading: false});}
  }, [path]);
  useEffect(() => {if (path) void load();}, [path, ...deps]);
  return {...state, reload: load};
}

export function Notice({error, ok}) {
  return <div className="team-notice" role="status" aria-live="polite">
    {error && <p className="team-error">{error}</p>}{ok && <p className="team-ok">{ok}</p>}
  </div>;
}
