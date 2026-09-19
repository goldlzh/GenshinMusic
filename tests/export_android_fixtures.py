"""Generate reference data from the desktop engine; never emits keyboard input."""
import itertools
from pathlib import Path
import random
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from midi_engine import *


def export(android_root):
    android_root = Path(android_root)
    resources = android_root / 'app/src/test/resources'
    resources.mkdir(parents=True, exist_ok=True)
    lines = []
    rng = random.Random(20260912)
    cases = [
        [MidiNote(t, t+length, pitch, 0, 0, mel, t+length+1) for t,length,pitch,mel in
         [(2,2,72,True),(2,1,48,False),(2.012,.8,52,False),(2.026,.9,55,False),
          (2.3,.2,84,False),(3,.5,72,True),(10,.001,60,True)]],
        [MidiNote(0,1,60,0,0,True,3),MidiNote(0,2,60,1,1,False,2),
         MidiNote(.005,1,72,0,0,True,1),MidiNote(.5,1.2,48,0,0,False,1.2)],
    ]
    for _ in range(10):
        t = 0
        notes = []
        for i in range(25):
            t += rng.choice([0, .005, .012, .015, .026, .04, .1, .5, 2])
            end = t + rng.choice([.001, .03, .2, 1, 3])
            notes.append(MidiNote(t, end, rng.randrange(30, 102), 0, 0, rng.random()<.4, end+rng.random()))
        cases.append(notes)
    case_id = 0
    for tagged in cases:
        for mode, fold, speed, pedal in itertools.product(DURATION_MODES, [False,True], [.5,1.,2.], [False,True]):
            offset = find_best_transpose(tagged, fold)
            lines.append('\t'.join(map(str,['C',case_id,mode,int(fold),speed,int(pedal),offset,
                        find_best_transpose(tagged,False),find_best_transpose(tagged,True)])))
            for n in tagged:
                lines.append('\t'.join(map(str,['N',n.start,n.end,n.pitch,int(n.melody),n.pedal_end])))
            schedule = build_schedule(finalize_notes(tagged,offset,fold,log=lambda _:None,duration_mode=mode,sustain_pedal=pedal),speed)
            for n in schedule:
                lines.append('\t'.join(map(str,['E',n.start,n.end,n.key,int(n.melody)])))
            case_id += 1
    (resources/'desktop_cases.tsv').write_text('\n'.join(lines),encoding='utf-8')
    lines=[]
    songs=sorted((android_root/'ref_python/GenshinMusic/songs').glob('*.mid'))
    for i,file in enumerate(songs):
        data=analyze_midi(file,log=lambda _:None)
        raw=[n for _,row in data['note_bearing'] for n in row]
        kept=auto_select_channels(data['channel_info'])
        tagged,_=detect_melody(filter_channels(data['note_bearing'],kept))
        gliss,_=detect_glissando(tagged)
        tagged=remove_glissando(tagged,gliss)
        fold=i%2==0; mode=list(DURATION_MODES)[i%4];speed=[.5,1.,2.][i%3]
        offset=find_best_transpose(tagged,fold)
        notes=build_schedule(finalize_notes(tagged,offset,fold,log=lambda _:None,duration_mode=mode,sustain_pedal=True),speed)
        row=[file.name,len(raw),sum(n.start for n in raw),sum(n.end for n in raw),sum(n.pedal_end for n in raw),
             find_best_transpose(tagged,False),find_best_transpose(tagged,True),mode,int(fold),speed,
             len(notes),max((n.end for n in notes),default=0),sum(n.start for n in notes),sum(n.end for n in notes)]
        lines.append('\t'.join(map(str,row)))
    (resources/'desktop_corpus.tsv').write_text('\n'.join(lines),encoding='utf-8')
    print(f'Exported {case_id} timing scenarios and {len(songs)} real songs.')


if __name__=='__main__': export(sys.argv[1])
