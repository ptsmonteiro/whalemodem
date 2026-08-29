"""Ordered complex-IQ narrow-FM channel and radio profiles."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Mapping, Sequence
import numpy as np
from scipy.signal import bilinear, butter, fftconvolve, firwin2, lfilter, minimum_phase, resample_poly, sosfilt
from .channel import (TAIL_MAX_SECONDS, TAIL_RELATIVE_TOLERANCE, ChannelResult,
                      _pole_tail_samples, _sos_tail_samples)

@dataclass(frozen=True)
class FmRfPath:
    delay_seconds: float = 0.; amplitude: float = 1.; phase_radians: float = 0.
    gain_flutter_depth: float = 0.; gain_flutter_hz: float = 0.
    phase_rate_hz: float = 0.; phase_flutter_radians: float = 0.; phase_flutter_hz: float = 0.
    def __post_init__(self):
        if not all(np.isfinite(v) for v in asdict(self).values()): raise ValueError("FM RF path parameters must be finite")
        if self.delay_seconds < 0 or self.amplitude <= 0: raise ValueError("FM RF path delay must be non-negative and amplitude positive")
        if not 0 <= self.gain_flutter_depth < 1: raise ValueError("FM RF gain flutter depth must lie in [0, 1)")
        if self.gain_flutter_hz < 0 or self.phase_flutter_hz < 0: raise ValueError("FM RF flutter rates must be non-negative")

@dataclass(frozen=True)
class FmRfInterference:
    offset_hz: float = 0.; power_db_relative: float = -20.; kind: str = "tone"
    modulation_hz: float = 1000.; deviation_hz: float = 1500.
    def __post_init__(self):
        if self.kind not in ("tone", "cochannel"): raise ValueError("RF interference kind must be tone or cochannel")
        if not all(np.isfinite(v) for v in (self.offset_hz,self.power_db_relative,self.modulation_hz,self.deviation_hz)): raise ValueError("RF interference parameters must be finite")
        if self.modulation_hz < 0 or self.deviation_hz < 0: raise ValueError("RF interference modulation values must be non-negative")

@dataclass(frozen=True)
class FmRadioPreset:
    name: str; audio_band_6db_hz: tuple[float,float]; audio_band_10db_hz: tuple[float,float]
    sample_clock_error_ppm: float; leading_mute_seconds: float; measured_delay_spread_ms: float; measurement_source: str
    def __post_init__(self):
        l10,h10=self.audio_band_10db_hz; l6,h6=self.audio_band_6db_hz
        if not 0 < l10 < l6 < h6 < h10: raise ValueError("FM preset audio bands must nest in frequency order")
        if self.leading_mute_seconds < 0 or self.measured_delay_spread_ms < 0: raise ValueError("FM preset timing measurements must be non-negative")

@dataclass(frozen=True)
class FmSyntheticProfile:
    name: str; tx_audio_band_hz: tuple[float,float]; rx_audio_band_hz: tuple[float,float]
    pre_emphasis_tau_seconds: float; de_emphasis_tau_seconds: float; tx_limiter_limit: float
    squelch_open_db: float; squelch_close_db: float; squelch_attack_seconds: float
    squelch_hang_seconds: float; squelch_close_seconds: float

_BS="experiments/ofdm/results/measurements/bandwidth.json"; _CS="scripts/measure_clock_offset.py (measurements in module docstring)"
FM_RADIO_PRESETS={
 "ic705_to_kg_uv9d":FmRadioPreset("ic705_to_kg_uv9d",(430.9,1905.5),(384.8,2453.2),-3.7,.110,.505,f"{_BS}; {_CS}; experiments/ofdm/screen_ofdm.py squelch measurement"),
 "kg_uv9d_to_ic705":FmRadioPreset("kg_uv9d_to_ic705",(425.1,1746.4),(363.,2372.3),3.1,0.,.815,f"{_BS}; {_CS}"),
 "vhf_bench_conservative":FmRadioPreset("vhf_bench_conservative",(430.9,1746.4),(384.8,2372.3),-3.7,.110,.815,"worst directional values from both VHF bench presets")}
FM_SYNTHETIC_PROFILES={
 "flat_nbfm":FmSyntheticProfile("flat_nbfm",(300.,3000.),(300.,3000.),75e-6,75e-6,.8,-18.,-22.,.015,.120,.010),
 "handheld_nbfm":FmSyntheticProfile("handheld_nbfm",(350.,2700.),(400.,2500.),75e-6,75e-6,.65,-15.,-20.,.080,.180,.020)}

class ComplexFmChannel:
    """Real-audio FM channel; C/N is complex-IQ power over full Nyquist."""
    STAGES=("tx_filter","pre_emphasis","tx_limiter","fm_modulation","rf_multipath_flutter","rf_noise_interference","if_filter_limiter_discriminator","de_emphasis","rx_filter","squelch","sample_clock")
    def __init__(self,sample_rate:int,carrier_to_noise_db:float,seed:int,*,deviation_hz=2500.,full_scale_audio=.6,rf_bandwidth_hz=7500.,rf_frequency_error_hz=0.,rf_paths:Sequence[FmRfPath]=(FmRfPath(),),rf_interference:Sequence[FmRfInterference]=(),preset:FmRadioPreset|None=None,synthetic_profile:FmSyntheticProfile|None=None,audio_band_6db_hz=None,audio_band_10db_hz=None,tx_audio_band_hz=None,rx_audio_band_hz=None,pre_emphasis_tau_seconds=0.,de_emphasis_tau_seconds=0.,tx_limiter_limit=None,sample_clock_error_ppm=0.,leading_mute_seconds=0.,trailing_mute_seconds=0.,audio_clip_limit=None,squelch_open_db=None,squelch_close_db=None,squelch_attack_seconds=0.,squelch_hang_seconds=0.,squelch_close_seconds=0.):
        if sample_rate<=0: raise ValueError("sample_rate must be positive")
        if preset and synthetic_profile: raise ValueError("measured preset and synthetic profile are exclusive")
        if synthetic_profile:
            p=synthetic_profile; tx_audio_band_hz=p.tx_audio_band_hz; rx_audio_band_hz=p.rx_audio_band_hz
            pre_emphasis_tau_seconds=p.pre_emphasis_tau_seconds; de_emphasis_tau_seconds=p.de_emphasis_tau_seconds; tx_limiter_limit=p.tx_limiter_limit
            squelch_open_db=p.squelch_open_db; squelch_close_db=p.squelch_close_db; squelch_attack_seconds=p.squelch_attack_seconds; squelch_hang_seconds=p.squelch_hang_seconds; squelch_close_seconds=p.squelch_close_seconds
        if preset:
            if audio_band_6db_hz is not None or audio_band_10db_hz is not None: raise ValueError("preset and explicit measured audio bands are exclusive")
            audio_band_6db_hz=preset.audio_band_6db_hz; audio_band_10db_hz=preset.audio_band_10db_hz; sample_clock_error_ppm=preset.sample_clock_error_ppm; leading_mute_seconds=preset.leading_mute_seconds
        nums=(carrier_to_noise_db,deviation_hz,full_scale_audio,rf_bandwidth_hz,rf_frequency_error_hz,sample_clock_error_ppm,leading_mute_seconds,trailing_mute_seconds,pre_emphasis_tau_seconds,de_emphasis_tau_seconds,squelch_attack_seconds,squelch_hang_seconds,squelch_close_seconds)
        if not all(np.isfinite(v) for v in nums): raise ValueError("FM channel parameters must be finite")
        if deviation_hz<=0 or full_scale_audio<=0: raise ValueError("FM deviation and full-scale audio must be positive")
        if not 0<rf_bandwidth_hz<sample_rate/2: raise ValueError("RF bandwidth must lie between zero and Nyquist")
        if not rf_paths: raise ValueError("FM channel requires at least one RF path")
        if any(v<0 for v in nums[-7:]): raise ValueError("FM timing constants must be non-negative")
        if (audio_band_6db_hz is None)!=(audio_band_10db_hz is None): raise ValueError("audio response requires both -6 and -10 dB bands")
        if any(v is not None and v<=0 for v in (tx_limiter_limit,audio_clip_limit)): raise ValueError("audio clip limits must be positive")
        if (squelch_open_db is None)!=(squelch_close_db is None) or (squelch_open_db is not None and squelch_open_db<=squelch_close_db): raise ValueError("squelch requires open threshold above close threshold")
        self.sample_rate=int(sample_rate); self.seed=int(seed); self.carrier_to_noise_db=float(carrier_to_noise_db); self.deviation_hz=float(deviation_hz); self.full_scale_audio=float(full_scale_audio); self.rf_bandwidth_hz=float(rf_bandwidth_hz); self.rf_frequency_error_hz=float(rf_frequency_error_hz)
        self.rf_paths=tuple(rf_paths); self.rf_interference=tuple(rf_interference); self.preset=preset; self.synthetic_profile=synthetic_profile; self.audio_band_6db_hz=audio_band_6db_hz; self.audio_band_10db_hz=audio_band_10db_hz; self.tx_audio_band_hz=tx_audio_band_hz; self.rx_audio_band_hz=rx_audio_band_hz
        self.pre_emphasis_tau_seconds=float(pre_emphasis_tau_seconds); self.de_emphasis_tau_seconds=float(de_emphasis_tau_seconds); self.tx_limiter_limit=tx_limiter_limit; self.audio_clip_limit=audio_clip_limit; self.sample_clock_error_ppm=float(sample_clock_error_ppm); self.leading_mute_seconds=float(leading_mute_seconds); self.trailing_mute_seconds=float(trailing_mute_seconds); self.squelch_open_db=squelch_open_db; self.squelch_close_db=squelch_close_db; self.squelch_attack_seconds=float(squelch_attack_seconds); self.squelch_hang_seconds=float(squelch_hang_seconds); self.squelch_close_seconds=float(squelch_close_seconds)
        self._rf_sos=butter(6,self.rf_bandwidth_hz,btype="lowpass",fs=sample_rate,output="sos"); self._tx_sos=self._band(tx_audio_band_hz); self._rx_sos=self._band(rx_audio_band_hz); self._audio_fir=self._measured_fir(); self._pre_ba=self._emphasis(self.pre_emphasis_tau_seconds,False); self._de_ba=self._emphasis(self.de_emphasis_tau_seconds,True)
        ratio=Fraction(1+self.sample_clock_error_ppm/1e6).limit_denominator(1_000_000); self._clock_up,self._clock_down=ratio.numerator,ratio.denominator; self._max_path_delay=max(round(p.delay_seconds*sample_rate) for p in self.rf_paths); self.reset()
        iir_tails=[_sos_tail_samples(self._rf_sos,self.sample_rate)]
        iir_tails += [_sos_tail_samples(s,self.sample_rate) for s in (self._tx_sos,self._rx_sos) if s is not None]
        iir_tails += [_pole_tail_samples(np.roots(a),self.sample_rate) for _,a in (self._pre_ba,self._de_ba)]
        self._tail_input_samples=min(round(TAIL_MAX_SECONDS*self.sample_rate),self._max_path_delay+len(self._audio_fir)-1+sum(iir_tails))
    @classmethod
    def from_preset(cls,sample_rate,preset,carrier_to_noise_db,seed,**kw):
        try:p=FM_RADIO_PRESETS[preset]
        except KeyError:raise ValueError(f"unknown FM radio preset {preset!r}; have {sorted(FM_RADIO_PRESETS)}") from None
        return cls(sample_rate,carrier_to_noise_db,seed,preset=p,**kw)
    @classmethod
    def from_profile(cls,sample_rate,profile,carrier_to_noise_db,seed,**kw):
        try:p=FM_SYNTHETIC_PROFILES[profile]
        except KeyError:raise ValueError(f"unknown FM synthetic profile {profile!r}; have {sorted(FM_SYNTHETIC_PROFILES)}") from None
        return cls(sample_rate,carrier_to_noise_db,seed,synthetic_profile=p,**kw)
    def _band(self,band):
        if band is None:return None
        if not 0<band[0]<band[1]<self.sample_rate/2:raise ValueError("audio filter band must lie in frequency order below Nyquist")
        return butter(4,band,btype="bandpass",fs=self.sample_rate,output="sos")
    def _emphasis(self,tau,inverse):
        if tau==0:return np.array([1.]),np.array([1.])
        return bilinear([1.] if inverse else [tau,1.],[tau,1.] if inverse else [1.],fs=self.sample_rate)
    def _measured_fir(self):
        if self.audio_band_6db_hz is None:return np.array([1.])
        l10,h10=self.audio_band_10db_hz;l6,h6=self.audio_band_6db_hz;cl=min(max(l6*1.5,l6+100),h6-100);ch=max(min(h6*.75,h6-100),cl+50)
        return minimum_phase(firwin2(2049,[0,l10,l6,cl,ch,h6,h10,self.sample_rate/2],[0,10**-.5,10**-.3,1,1,10**-.3,10**-.5,0],fs=self.sample_rate),method="homomorphic",half=False)
    def reset(self):
        self._rng=np.random.default_rng(self.seed);self._phase=0.;self._sample_index=0;self._rf_zi=np.zeros((len(self._rf_sos),2),complex);self._tx_zi=None if self._tx_sos is None else np.zeros((len(self._tx_sos),2));self._rx_zi=None if self._rx_sos is None else np.zeros((len(self._rx_sos),2));self._pre_zi=np.zeros(max(map(len,self._pre_ba))-1);self._de_zi=np.zeros(max(map(len,self._de_ba))-1);self._audio_history=np.zeros(len(self._audio_fir)-1);self._previous_iq=1+0j;self._interference_phases=np.zeros(len(self.rf_interference));self._squelch_open=False
    def _modulate(self,a):
        f=self.rf_frequency_error_hz+self.deviation_hz*a/self.full_scale_audio;p=self._phase+2*np.pi*np.cumsum(f)/self.sample_rate
        if len(p):self._phase=float(np.remainder(p[-1],2*np.pi))
        return np.exp(1j*p)
    def _squelch(self,a,level):
        if self.squelch_open_db is None:return a,0
        opened=level>=self.squelch_open_db or (self._squelch_open and level>=self.squelch_close_db);mask=np.ones(len(a)) if opened else np.zeros(len(a))
        if opened and not self._squelch_open:
            k=min(len(a),round(self.squelch_attack_seconds*self.sample_rate));mask[:k]=np.linspace(0,1,k,endpoint=False) if k else 1
        elif not opened and self._squelch_open:
            h=min(len(a),round(self.squelch_hang_seconds*self.sample_rate));c=min(len(a)-h,round(self.squelch_close_seconds*self.sample_rate));mask[:h]=1
            if c:mask[h:h+c]=np.linspace(1,0,c,endpoint=False)
        self._squelch_open=opened;return a*mask,int(np.count_nonzero(mask==0))
    def _process(self,audio,continuation=False):
        x=np.asarray(audio,dtype=float)
        if x.ndim!=1:raise ValueError("channel audio must be a mono one-dimensional array")
        if not len(x):return ChannelResult(np.zeros(0,np.float32),{})
        if self._tx_sos is not None:x,self._tx_zi=sosfilt(self._tx_sos,x,zi=self._tx_zi)
        x,self._pre_zi=lfilter(*self._pre_ba,x,zi=self._pre_zi);txclip=0
        if self.tx_limiter_limit is not None:txclip=int(np.count_nonzero(abs(x)>self.tx_limiter_limit));x=np.clip(x,-self.tx_limiter_limit,self.tx_limiter_limit)
        tx=self._modulate(x);rf=np.zeros(len(tx)+self._max_path_delay,complex);pp=sum(p.amplitude**2 for p in self.rf_paths)
        for p in self.rf_paths:
            d=round(p.delay_seconds*self.sample_rate);t=(self._sample_index+np.arange(len(tx)))/self.sample_rate;g=p.amplitude/np.sqrt(pp)*(1+p.gain_flutter_depth*np.sin(2*np.pi*p.gain_flutter_hz*t));ph=p.phase_radians+2*np.pi*p.phase_rate_hz*t+p.phase_flutter_radians*np.sin(2*np.pi*p.phase_flutter_hz*t);rf[d:d+len(tx)]+=g*np.exp(1j*ph)*tx
        cp=float(np.mean(abs(rf)**2));n=np.arange(len(rf));ip=0.
        for i,s in enumerate(self.rf_interference):
            ph=self._interference_phases[i]+2*np.pi*s.offset_hz*(n+1)/self.sample_rate
            if s.kind=="cochannel" and s.modulation_hz:ph+=s.deviation_hz/s.modulation_hz*np.sin(2*np.pi*s.modulation_hz*(self._sample_index+n)/self.sample_rate)
            tone=np.sqrt(cp*10**(s.power_db_relative/10))*np.exp(1j*ph);rf+=tone;ip+=float(np.mean(abs(tone)**2));self._interference_phases[i]=float(np.remainder(ph[-1],2*np.pi))
        requested=cp/10**(self.carrier_to_noise_db/10);sigma=np.sqrt(requested/2);noise=self._rng.normal(0,sigma,len(rf))+1j*self._rng.normal(0,sigma,len(rf));npow=float(np.mean(abs(noise)**2));f,self._rf_zi=sosfilt(self._rf_sos,rf+noise,zi=self._rf_zi);level=10*np.log10(max(float(np.mean(abs(f)**2)),1e-30));limited=f/np.maximum(abs(f),1e-12);prev=np.concatenate(([self._previous_iq],limited[:-1]));self._previous_iq=limited[-1];y=np.angle(limited*np.conj(prev))*self.sample_rate/(2*np.pi)/self.deviation_hz*self.full_scale_audio;y,self._de_zi=lfilter(*self._de_ba,y,zi=self._de_zi)
        if self._rx_sos is not None:y,self._rx_zi=sosfilt(self._rx_sos,y,zi=self._rx_zi)
        if len(self._audio_fir)>1:
            e=np.concatenate((self._audio_history,y));z=fftconvolve(e,self._audio_fir,mode="full");h=len(self._audio_fir)-1;y=z[h:h+len(y)];self._audio_history=e[-h:]
        rxclip=0
        if self.audio_clip_limit is not None:rxclip=int(np.count_nonzero(abs(y)>self.audio_clip_limit));y=np.clip(y,-self.audio_clip_limit,self.audio_clip_limit)
        y,sqmuted=self._squelch(y,level);lead=0 if continuation else min(round(self.leading_mute_seconds*self.sample_rate),len(y));trail=0 if continuation else min(round(self.trailing_mute_seconds*self.sample_rate),len(y)-lead);y[:lead]=0
        if trail:y[-trail:]=0
        if self._clock_up!=self._clock_down:
            y=resample_poly(y,self._clock_up,self._clock_down)
            # Preserve hard radio muting after the antialiasing kernel.  The
            # clock remains the last transformation; this only reapplies the
            # already-decided squelch gate so it cannot ring into mute time.
            out_lead=min(round(lead*self._clock_up/self._clock_down),len(y));out_trail=min(round(trail*self._clock_up/self._clock_down),len(y)-out_lead);y[:out_lead]=0
            if out_trail:y[-out_trail:]=0
        self._sample_index+=len(tx);cn=float("inf") if npow==0 else 10*np.log10(cp/npow)
        return ChannelResult(y.astype(np.float32),{"rf_carrier_to_noise_db":self.carrier_to_noise_db,"realized_rf_carrier_to_noise_db":float(cn),"rf_carrier_power":cp,"rf_noise_power":npow,"rf_interference_power":ip,"rf_frequency_error_hz":self.rf_frequency_error_hz,"deviation_hz":self.deviation_hz,"if_level_db":float(level),"squelch_open":self._squelch_open,"muted_samples":lead+trail+sqmuted,"leading_muted_samples":lead,"trailing_muted_samples":trail,"sample_clock_error_ppm":self.sample_clock_error_ppm,"tx_limited_samples":txclip,"clipped_audio_samples":rxclip})
    def process(self,audio):
        return self._process(audio)
    def drain(self,audio=None):
        x=np.zeros(0,np.float32) if audio is None else np.asarray(audio,dtype=np.float32)
        if x.ndim!=1:raise ValueError("channel audio must be a mono one-dimensional array")
        parts=[]
        if len(x):parts.append(self._process(x,continuation=True).audio)
        parts.append(self._process(np.zeros(self._tail_input_samples,np.float32),continuation=True).audio)
        return ChannelResult(np.concatenate(parts) if parts else np.zeros(0,np.float32),{"input_samples":len(x),"tail_input_samples":self._tail_input_samples,"relative_tolerance":TAIL_RELATIVE_TOLERANCE,"max_tail_seconds":TAIL_MAX_SECONDS})
    def describe(self)->Mapping[str,object]:
        return {"type":"complex_fm","sample_rate":self.sample_rate,"seed":self.seed,"preset":None if self.preset is None else self.preset.name,"synthetic_profile":None if self.synthetic_profile is None else self.synthetic_profile.name,"stages":list(self.STAGES),"carrier_to_noise_db":self.carrier_to_noise_db,"carrier_to_noise_reference":"complex_iq_full_nyquist","deviation_hz":self.deviation_hz,"full_scale_audio":self.full_scale_audio,"rf_bandwidth_hz":self.rf_bandwidth_hz,"rf_frequency_error_hz":self.rf_frequency_error_hz,"tx_audio_band_hz":self.tx_audio_band_hz,"rx_audio_band_hz":self.rx_audio_band_hz,"audio_band_6db_hz":self.audio_band_6db_hz,"audio_band_10db_hz":self.audio_band_10db_hz,"pre_emphasis_tau_seconds":self.pre_emphasis_tau_seconds,"de_emphasis_tau_seconds":self.de_emphasis_tau_seconds,"tx_limiter_limit":self.tx_limiter_limit,"sample_clock_error_ppm":self.sample_clock_error_ppm,"leading_mute_seconds":self.leading_mute_seconds,"trailing_mute_seconds":self.trailing_mute_seconds,"squelch_open_db":self.squelch_open_db,"squelch_close_db":self.squelch_close_db,"squelch_attack_seconds":self.squelch_attack_seconds,"squelch_hang_seconds":self.squelch_hang_seconds,"squelch_close_seconds":self.squelch_close_seconds,"measured_delay_spread_ms":None if self.preset is None else self.preset.measured_delay_spread_ms,"measurement_source":None if self.preset is None else self.preset.measurement_source,"rf_paths":[asdict(p) for p in self.rf_paths],"rf_interference":[asdict(i) for i in self.rf_interference]}
